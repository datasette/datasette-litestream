from datasette import hookimpl, Forbidden
from datasette.permissions import Action
from datasette.utils import StartupError
from datasette.utils.asgi import Response
from pathlib import Path
import asyncio
import atexit
import httpx
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from prometheus_client.parser import text_string_to_metric_families

from ._client import LitestreamClient, LitestreamControlError
from ._vite import vite_entry


def load_credentials_from_file(path: str) -> dict:
    """Load credentials from a JSON file."""
    with open(path) as f:
        data = json.load(f)
    if "access-key-id" not in data or "secret-access-key" not in data:
        raise StartupError(
            f"Credentials file {path} must contain 'access-key-id' and 'secret-access-key'"
        )
    result = {
        "access-key-id": data["access-key-id"],
        "secret-access-key": data["secret-access-key"],
    }
    if "session-token" in data:
        result["session-token"] = data["session-token"]
    return result


def load_credentials_from_command(command: str) -> dict:
    """Execute a command and parse its JSON output for credentials."""
    args = shlex.split(command)
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise StartupError(
            f"Credentials command failed with return code {result.returncode}: {result.stderr}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise StartupError(f"Credentials command output is not valid JSON: {e}")
    if "access-key-id" not in data or "secret-access-key" not in data:
        raise StartupError(
            "Credentials command output must contain 'access-key-id' and 'secret-access-key'"
        )
    creds = {
        "access-key-id": data["access-key-id"],
        "secret-access-key": data["secret-access-key"],
    }
    if "session-token" in data:
        creds["session-token"] = data["session-token"]
    return creds


def get_dynamic_credentials(plugin_config: dict) -> dict:
    """Get credentials from file or command if configured."""
    credentials_file = plugin_config.get("credentials-file")
    credentials_command = plugin_config.get("credentials-command")

    if credentials_file:
        return load_credentials_from_file(credentials_file)
    elif credentials_command:
        return load_credentials_from_command(credentials_command)
    return None


def credentials_hash(creds: dict) -> str:
    """Return a hash string for comparing credentials."""
    if creds is None:
        return ""
    return json.dumps(creds, sort_keys=True)


REDACTED_KEYS = {"secret-access-key", "session-token"}


def redact_credentials(config: dict) -> dict:
    """Return a copy of config with sensitive credentials redacted."""
    redacted = {}
    for key, value in config.items():
        if key in REDACTED_KEYS:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


def credentials_env(creds: dict) -> dict:
    """Translate a credentials dict into AWS_* environment variables.

    litestream >= 0.5 picks up S3 credentials from the daemon's environment when
    a database is registered with an ``s3://`` replica URL, so we always pass
    credentials this way rather than embedding them in the config file. We set
    the AWS_* names directly (not LITESTREAM_*) so they take effect regardless of
    litestream's env-precedence rules, and so session tokens work consistently.
    """
    if not creds:
        return {}
    env = {}
    if "access-key-id" in creds:
        env["AWS_ACCESS_KEY_ID"] = creds["access-key-id"]
    if "secret-access-key" in creds:
        env["AWS_SECRET_ACCESS_KEY"] = creds["secret-access-key"]
    if "session-token" in creds:
        env["AWS_SESSION_TOKEN"] = creds["session-token"]
    return env


def expand_replica_template(template: str, db_name: str, db_path: Path) -> str:
    """Expand the $DB_NAME / $DB_DIRECTORY / $PWD variables in a replica URL."""
    return (
        template.replace("$DB_NAME", db_name)
        .replace("$DB_DIRECTORY", str(Path(db_path).resolve().parent))
        .replace("$PWD", os.getcwd())
    )


def resolve_replica_url(db_name, db_path, plugin_config_db, all_replicate):
    """Determine the single replica URL for a database, or None to skip it.

    litestream 0.5 replicates each database to exactly one destination, so we
    resolve a single URL. Precedence:
      1. db-level config ``replica`` (a single URL string)
      2. db-level config ``replicas`` (deprecated list; first entry is used)
      3. top-level ``all-replicate`` template (string, or first entry of a list)
    """
    template = None
    if plugin_config_db:
        if plugin_config_db.get("replica"):
            template = plugin_config_db["replica"]
        elif plugin_config_db.get("replicas"):
            replicas = plugin_config_db["replicas"]
            first = replicas[0]
            template = first.get("url") if isinstance(first, dict) else first
    if template is None and all_replicate is not None:
        if isinstance(all_replicate, (list, tuple)):
            template = all_replicate[0] if all_replicate else None
        else:
            template = all_replicate
    if template is None:
        return None
    return expand_replica_template(template, db_name, db_path)


class LitestreamProcess:
    """Manages a long-lived ``litestream replicate`` daemon.

    The daemon is started once with its control socket enabled and no databases
    in its config file. Databases are added and removed at runtime over the
    control socket (register/unregister), so no daemon restart is needed when the
    set of replicated databases changes.
    """

    def __init__(self):
        self.process = None
        # Control socket path and a client bound to it.
        self.socket_dir = None
        self.socket_path = None
        self.client = None
        # The daemon config (dict) we wrote out, kept for the status page.
        self.daemon_config = None
        # Metrics/pprof bind address, if configured.
        self.metrics_addr = None
        # Credentials and a hash for change detection.
        self.credentials = None
        self.current_credentials_hash = None
        # path (str) -> replica_url for every database we have registered.
        self.registered = {}
        # Temp files.
        self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=True)
        self.configfile = None
        # atexit handler (stored so we can unregister it) and refresh task.
        self._atexit_handler = None
        self._refresh_task = None

    # --- Daemon lifecycle -------------------------------------------------

    def start_daemon(self):
        """Start the litestream daemon with the control socket enabled."""
        litestream_path = resolve_litestream_path()

        self.socket_dir = tempfile.mkdtemp(prefix="datasette-litestream-")
        self.socket_path = str(Path(self.socket_dir) / "litestream.sock")

        self.daemon_config = {
            "socket": {
                "enabled": True,
                "path": self.socket_path,
                "permissions": 0o600,
            }
        }
        if self.metrics_addr:
            self.daemon_config["addr"] = self.metrics_addr

        self.configfile = tempfile.NamedTemporaryFile(suffix=".yml", delete=False)
        with self.configfile as f:
            f.write(bytes(json.dumps(self.daemon_config), "utf-8"))
            config_path = Path(f.name)

        env = os.environ.copy()
        env.update(credentials_env(self.credentials))

        self.process = subprocess.Popen(
            [litestream_path, "replicate", "-config", str(config_path)],
            stderr=self.logfile,
            env=env,
        )

        # Wait briefly to catch instant failures (typically config typos).
        time.sleep(0.5)
        status = self.process.poll()
        if status is not None:
            logs = open(self.logfile.name, "r").read()
            raise Exception(
                f"datasette-litestream litestream process failed with return code {status}. Logs:"
                + logs
            )

        self._wait_for_socket()
        self.client = LitestreamClient(self.socket_path)

        # Sometimes Popen doesn't die on exit, so explicitly kill it on exit.
        def onexit():
            if self.process:
                self.process.kill()
            if self.configfile and Path(self.configfile.name).exists():
                Path(self.configfile.name).unlink()

        self._atexit_handler = onexit
        atexit.register(onexit)

    def _wait_for_socket(self, timeout=5.0):
        """Block until the control socket file appears (or the process dies)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(self.socket_path).exists():
                return
            if self.process.poll() is not None:
                logs = open(self.logfile.name, "r").read()
                raise Exception(
                    "datasette-litestream litestream process exited before opening "
                    "its control socket. Logs:" + logs
                )
            time.sleep(0.05)
        raise Exception(
            f"datasette-litestream: control socket {self.socket_path} did not appear "
            f"within {timeout}s"
        )

    def stop_daemon(self):
        """Gracefully stop the litestream daemon and clean up temp files."""
        if self._atexit_handler:
            atexit.unregister(self._atexit_handler)
            self._atexit_handler = None

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

        if self.configfile and Path(self.configfile.name).exists():
            Path(self.configfile.name).unlink(missing_ok=True)
            self.configfile = None

        if self.socket_path and Path(self.socket_path).exists():
            Path(self.socket_path).unlink(missing_ok=True)
        if self.socket_dir and Path(self.socket_dir).exists():
            try:
                Path(self.socket_dir).rmdir()
            except OSError:
                pass
        self.client = None

    # --- Runtime database management -------------------------------------

    def register_db(self, db_path: str, replica_url: str) -> dict:
        """Register a database for replication over the control socket."""
        result = self.client.register(db_path, replica_url)
        self.registered[str(db_path)] = replica_url
        return result

    def unregister_db(self, db_path: str, timeout=None) -> dict:
        """Unregister a database; the daemon performs a final sync first."""
        result = self.client.unregister(db_path, timeout=timeout)
        self.registered.pop(str(db_path), None)
        return result

    def reregister_all(self):
        """Re-register every known database (used after a daemon restart)."""
        for db_path, replica_url in list(self.registered.items()):
            self.client.register(db_path, replica_url)

    # --- Credential rotation ---------------------------------------------

    def update_credentials(self, new_creds: dict):
        self.credentials = new_creds
        self.current_credentials_hash = credentials_hash(new_creds)

    def restart_with_new_credentials(self, new_creds: dict):
        """Restart the daemon with new credentials and re-register databases.

        Credentials reach litestream through the daemon's environment, which a
        running process can't change, so a rotation requires a restart. We
        preserve the set of replicated databases by re-registering them.
        """
        registered = dict(self.registered)
        self.stop_daemon()
        self.update_credentials(new_creds)
        self.start_daemon()
        self.registered = registered
        self.reregister_all()


# global variable that tracks each datasette-litestream instance. There is usually just 1,
# but in test suites there may be multiple Datasette instances.
# The keys are a UUID generated in the startup hook, values are a LitestreamProcess
processes = {}

# The uuid generated at startup is stored on the datasette object, stored in this key attr.
# Meant so we can retrieve it in the separate litestream_status route
DATASETTE_LITESTREAM_PROCESS_KEY = "__DATASETTE_LITESTREAM_PROCESS_KEY__"


def get_process(datasette):
    """Return the LitestreamProcess for this Datasette instance, or None."""
    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, None)
    if startup_id is None:
        return None
    return processes.get(startup_id)


def resolve_litestream_path():
    """resolives the full path to a litestream binary. Hopefully is bundled in the installed wheel"""

    # Allow an explicit override, useful for development and tests.
    override = os.environ.get("DATASETTE_LITESTREAM_BINARY")
    if override:
        return override

    # First try to see if litestream was bundled with that package, in a pre-built wheel
    wheel_path = Path(__file__).resolve().parent / "bin" / "litestream"
    if wheel_path.exists():
        return str(wheel_path)

    # Fallback to any litestream binary on the system.
    executable_path = shutil.which("litestream")

    if executable_path is None:
        raise Exception("litestream not found.")

    return str(executable_path)


@hookimpl
def register_actions(datasette):
    return [
        Action(
            name="litestream-view-status",
            description="View litestream statistics and status updates.",
        ),
        Action(
            name="litestream-manage",
            description="Add or remove databases from litestream replication at runtime.",
        ),
    ]


@hookimpl
def menu_links(datasette, actor):
    async def inner():
        if (
            await datasette.allowed(actor=actor, action="litestream-view-status")
            # TODO why is this needed?
            and datasette.plugin_config("datasette-litestream") is not None
        ):
            return [
                {
                    "href": datasette.urls.path("/-/litestream"),
                    "label": "Litestream",
                },
            ]

    return inner


@hookimpl
def extra_template_vars(datasette):
    """Expose the Vite entry helper to templates as datasette_litestream_vite_entry."""

    def entry(entrypoint):
        return vite_entry(datasette, entrypoint)

    return {"datasette_litestream_vite_entry": entry}


async def credential_refresh_loop(
    startup_id: str, plugin_config: dict, interval_seconds: int
):
    """Background task that periodically checks for credential changes."""
    global processes

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            litestream_process = processes.get(startup_id)
            if litestream_process is None:
                return  # Process no longer exists

            new_creds = get_dynamic_credentials(plugin_config)
            if new_creds is None:
                continue

            new_hash = credentials_hash(new_creds)
            if new_hash != litestream_process.current_credentials_hash:
                print(
                    f"datasette-litestream: credentials changed, restarting litestream",
                    file=sys.stderr,
                )
                await asyncio.to_thread(
                    litestream_process.restart_with_new_credentials, new_creds
                )

        except Exception as e:
            print(
                f"datasette-litestream: fatal error refreshing credentials: {e}",
                file=sys.stderr,
            )
            sys.exit(1)


@hookimpl
def startup(datasette):
    global processes

    litestream_process = LitestreamProcess()

    plugin_config_top = datasette.plugin_config("datasette-litestream") or {}

    # Validate mutually exclusive credential options
    credentials_file = plugin_config_top.get("credentials-file")
    credentials_command = plugin_config_top.get("credentials-command")
    credentials_refresh_interval = plugin_config_top.get("credentials-refresh-interval")

    if credentials_file and credentials_command:
        raise StartupError(
            "datasette-litestream: cannot specify both 'credentials-file' and 'credentials-command'"
        )

    uses_dynamic_credentials = credentials_file or credentials_command

    if uses_dynamic_credentials and not credentials_refresh_interval:
        raise StartupError(
            "datasette-litestream: 'credentials-refresh-interval' is required when using "
            "'credentials-file' or 'credentials-command'"
        )

    # Load credentials from file/command or from static config
    creds = {}
    if uses_dynamic_credentials:
        try:
            dynamic_creds = get_dynamic_credentials(plugin_config_top)
            creds = dynamic_creds
        except Exception as e:
            raise StartupError(
                f"datasette-litestream: failed to load initial credentials: {e}"
            ) from e
    else:
        if "access-key-id" in plugin_config_top:
            creds["access-key-id"] = plugin_config_top.get("access-key-id")
        if "secret-access-key" in plugin_config_top:
            creds["secret-access-key"] = plugin_config_top.get("secret-access-key")
        if "session-token" in plugin_config_top:
            creds["session-token"] = plugin_config_top.get("session-token")

    litestream_process.credentials = creds or None
    litestream_process.current_credentials_hash = credentials_hash(creds or None)

    if "metrics-addr" in plugin_config_top:
        litestream_process.metrics_addr = plugin_config_top.get("metrics-addr")

    all_replicate = plugin_config_top.get("all-replicate")

    # Work out which databases to replicate at startup.
    initial = []  # list of (db_path_str, replica_url)
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue

        db_path = Path(db.path)
        plugin_config_db = datasette.plugin_config(
            "datasette-litestream", db_name, fallback=False
        )

        # skip this DB if "all-replicate" was not defined or no db-level config was given
        if plugin_config_db is None and all_replicate is None:
            continue

        replica_url = resolve_replica_url(
            db_name, db_path, plugin_config_db, all_replicate
        )
        if replica_url is None:
            continue

        initial.append((str(db_path.resolve()), replica_url))

    # don't run litestream if no top-level or db-level datasette-litestream config was given
    if not plugin_config_top and len(initial) == 0:
        return

    startup_id = str(uuid.uuid4())
    processes[startup_id] = litestream_process
    setattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, startup_id)

    litestream_process.start_daemon()
    for db_path, replica_url in initial:
        litestream_process.register_db(db_path, replica_url)

    # Schedule credential refresh if using dynamic credentials
    if uses_dynamic_credentials:
        litestream_process._refresh_task = asyncio.create_task(
            credential_refresh_loop(
                startup_id, plugin_config_top, credentials_refresh_interval
            )
        )


@hookimpl
def register_routes():
    return [
        (r"^/-/litestream$", litestream_admin_page),
        (r"^/-/litestream/api/status$", litestream_api_status),
        (r"^/-/litestream/api/sync$", litestream_api_sync),
        (r"^/-/litestream/api/start$", litestream_api_start),
        (r"^/-/litestream/api/stop$", litestream_api_stop),
        (r"^/-/litestream-status$", litestream_status),
        (r"^/-/litestream/register$", litestream_register),
        (r"^/-/litestream/unregister$", litestream_unregister),
    ]


def _resolve_db_path(datasette, db_name):
    """Return the resolved file path for a Datasette database name, or None."""
    db = datasette.databases.get(db_name)
    if db is None or db.path is None:
        return None
    return str(Path(db.path).resolve())


async def _read_json_body(request):
    body = await request.post_body()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _suggested_replica(datasette, db_name, db_path):
    """Resolve the configured replica URL for a database, if any."""
    plugin_config_db = datasette.plugin_config(
        "datasette-litestream", db_name, fallback=False
    )
    all_replicate = (datasette.plugin_config("datasette-litestream") or {}).get(
        "all-replicate"
    )
    return resolve_replica_url(db_name, Path(db_path), plugin_config_db, all_replicate)


async def _build_status(datasette, litestream_process, can_manage):
    """Assemble the JSON status payload consumed by the admin UI."""
    # Map litestream's absolute paths back to Datasette database names.
    name_by_path = {}
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        name_by_path[str(Path(db.path).resolve())] = db_name

    daemon = None
    managed = []
    socket_error = None
    registered_paths = set()
    if litestream_process.client is not None:
        try:
            daemon = await asyncio.to_thread(litestream_process.client.info)
            databases = await asyncio.to_thread(
                litestream_process.client.list_databases
            )
            for entry in databases:
                path = entry.get("path")
                registered_paths.add(path)
                managed.append(
                    {
                        "database": name_by_path.get(path),
                        "path": path,
                        "status": entry.get("status"),
                        "last_sync_at": entry.get("last_sync_at"),
                        "replica": litestream_process.registered.get(path),
                    }
                )
        except Exception as e:
            socket_error = str(e)

    # Attached, file-backed Datasette databases not currently replicating.
    available = []
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        resolved = str(Path(db.path).resolve())
        if resolved in registered_paths:
            continue
        available.append(
            {
                "database": db_name,
                "path": resolved,
                "suggested_replica": _suggested_replica(datasette, db_name, resolved),
            }
        )

    return {
        "running": True,
        "can_manage": can_manage,
        "metrics_enabled": litestream_process.metrics_addr is not None,
        "daemon": daemon,
        "socket_error": socket_error,
        "databases": managed,
        "available": available,
    }


async def litestream_admin_page(scope, receive, datasette, request):
    """GET /-/litestream — the Svelte admin UI (read-only without manage)."""
    if not await datasette.allowed(
        actor=request.actor, action="litestream-view-status"
    ):
        raise Forbidden("Permission denied for litestream-view-status")

    can_manage = await datasette.allowed(
        actor=request.actor, action="litestream-manage"
    )
    return Response.html(
        await datasette.render_template(
            "litestream_admin.html",
            context={
                "page_data": {
                    "can_manage": bool(can_manage),
                    "actor": request.actor,
                },
            },
            request=request,
        )
    )


async def litestream_api_status(scope, receive, datasette, request):
    """GET /-/litestream/api/status — JSON status snapshot for the UI to poll."""
    if not await datasette.allowed(
        actor=request.actor, action="litestream-view-status"
    ):
        raise Forbidden("Permission denied for litestream-view-status")

    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json({"running": False})

    can_manage = await datasette.allowed(
        actor=request.actor, action="litestream-manage"
    )
    return Response.json(
        await _build_status(datasette, litestream_process, bool(can_manage))
    )


async def _db_action(datasette, request, method_name, **kwargs):
    """Shared handler for sync/start/stop: resolve the db and call the client."""
    if request.method != "POST":
        return Response.json({"ok": False, "error": "POST required"}, status=405)
    if not await datasette.allowed(actor=request.actor, action="litestream-manage"):
        raise Forbidden("Permission denied for litestream-manage")

    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    data = await _read_json_body(request)
    if data is None:
        return Response.json({"ok": False, "error": "invalid JSON body"}, status=400)

    db_name = data.get("database")
    if not db_name:
        return Response.json(
            {"ok": False, "error": "'database' is required"}, status=400
        )

    db_path = _resolve_db_path(datasette, db_name)
    if db_path is None:
        return Response.json(
            {"ok": False, "error": f"unknown or in-memory database: {db_name}"},
            status=404,
        )

    method = getattr(litestream_process.client, method_name)
    try:
        result = await asyncio.to_thread(method, db_path, **kwargs)
    except LitestreamControlError as e:
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details}, status=502
        )
    return Response.json({"ok": True, "database": db_name, "result": result})


async def litestream_api_sync(scope, receive, datasette, request):
    """POST /-/litestream/api/sync  {"database": "<name>"}"""
    return await _db_action(datasette, request, "sync", wait=True)


async def litestream_api_start(scope, receive, datasette, request):
    """POST /-/litestream/api/start  {"database": "<name>"}"""
    return await _db_action(datasette, request, "start")


async def litestream_api_stop(scope, receive, datasette, request):
    """POST /-/litestream/api/stop  {"database": "<name>"}"""
    return await _db_action(datasette, request, "stop")


async def litestream_register(scope, receive, datasette, request):
    """POST /-/litestream/register  {"database": "<name>", "replica": "<url?>"}

    Registers a currently-attached Datasette database with the running litestream
    daemon at runtime. The replica URL may be supplied in the body, otherwise it
    is resolved from the plugin's ``all-replicate`` / db-level config.
    """
    if request.method != "POST":
        return Response.json({"ok": False, "error": "POST required"}, status=405)
    if not await datasette.allowed(actor=request.actor, action="litestream-manage"):
        raise Forbidden("Permission denied for litestream-manage")

    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    data = await _read_json_body(request)
    if data is None:
        return Response.json({"ok": False, "error": "invalid JSON body"}, status=400)

    db_name = data.get("database")
    if not db_name:
        return Response.json(
            {"ok": False, "error": "'database' is required"}, status=400
        )

    db_path = _resolve_db_path(datasette, db_name)
    if db_path is None:
        return Response.json(
            {"ok": False, "error": f"unknown or in-memory database: {db_name}"},
            status=404,
        )

    replica_url = data.get("replica")
    if not replica_url:
        plugin_config_db = datasette.plugin_config(
            "datasette-litestream", db_name, fallback=False
        )
        all_replicate = (
            datasette.plugin_config("datasette-litestream") or {}
        ).get("all-replicate")
        replica_url = resolve_replica_url(
            db_name, Path(db_path), plugin_config_db, all_replicate
        )
    if not replica_url:
        return Response.json(
            {
                "ok": False,
                "error": "no replica URL provided or configured for this database",
            },
            status=400,
        )

    try:
        result = await asyncio.to_thread(
            litestream_process.register_db, db_path, replica_url
        )
    except LitestreamControlError as e:
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details},
            status=502,
        )

    return Response.json(
        {
            "ok": True,
            "database": db_name,
            "path": db_path,
            "replica": replica_url,
            "status": result.get("status"),
        }
    )


async def litestream_unregister(scope, receive, datasette, request):
    """POST /-/litestream/unregister  {"database": "<name>", "timeout": <int?>}

    Removes a database from litestream replication at runtime. The daemon
    performs a final sync to the replica before dropping it.
    """
    if request.method != "POST":
        return Response.json({"ok": False, "error": "POST required"}, status=405)
    if not await datasette.allowed(actor=request.actor, action="litestream-manage"):
        raise Forbidden("Permission denied for litestream-manage")

    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    data = await _read_json_body(request)
    if data is None:
        return Response.json({"ok": False, "error": "invalid JSON body"}, status=400)

    db_name = data.get("database")
    if not db_name:
        return Response.json(
            {"ok": False, "error": "'database' is required"}, status=400
        )

    # Prefer the attached database's path; fall back to any path we registered
    # under this name in case the database was already detached from Datasette.
    db_path = _resolve_db_path(datasette, db_name)
    if db_path is None:
        return Response.json(
            {"ok": False, "error": f"unknown or in-memory database: {db_name}"},
            status=404,
        )

    timeout = data.get("timeout")
    try:
        result = await asyncio.to_thread(
            litestream_process.unregister_db, db_path, timeout
        )
    except LitestreamControlError as e:
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details},
            status=502,
        )

    return Response.json(
        {
            "ok": True,
            "database": db_name,
            "path": db_path,
            "status": result.get("status"),
            "txid": result.get("txid"),
        }
    )


async def litestream_status(scope, receive, datasette, request):
    if not await datasette.allowed(
        actor=request.actor, action="litestream-view-status"
    ):
        raise Forbidden("Permission denied for litestream-view-status")

    litestream_process = get_process(datasette)

    if litestream_process is None:
        return Response.html("<h1>Litestream not running</h1>")

    # Map litestream's absolute database paths back to Datasette names.
    db_name_lookup = {}
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        db_name_lookup[str(Path(db.path).resolve())] = db_name

    # Live daemon state from the control socket (best effort).
    daemon_info = None
    managed = []
    socket_error = None
    if litestream_process.client is not None:
        try:
            daemon_info = await asyncio.to_thread(litestream_process.client.info)
            databases = await asyncio.to_thread(
                litestream_process.client.list_databases
            )
            for entry in databases:
                managed.append(
                    {
                        "database": db_name_lookup.get(entry.get("path")),
                        "path": entry.get("path"),
                        "status": entry.get("status"),
                        "last_sync_at": entry.get("last_sync_at"),
                    }
                )
        except LitestreamControlError as e:
            socket_error = str(e)
        except Exception as e:
            socket_error = str(e)

    replica_operations = {"bytes": [], "total": []}
    metrics_by_db = {}
    go_stats = {}

    metrics_enabled = litestream_process.metrics_addr is not None

    if metrics_enabled:
        addr = litestream_process.metrics_addr
        # TODO detect when non-localhost addresses are used
        try:
            metrics_page = httpx.get(f"http://localhost{addr}/metrics").text
        except Exception:
            metrics_page = ""

        for family in text_string_to_metric_families(metrics_page):
            for sample in family.samples:
                # litestream 0.5 renamed the bytes counter (dropped the _total suffix).
                if sample.name in (
                    "litestream_replica_operation_bytes",
                    "litestream_replica_operation_bytes_total",
                ):
                    replica_operations["bytes"].append(
                        {**sample.labels, "value": sample.value}
                    )
                elif sample.name == "litestream_replica_operation_total":
                    replica_operations["total"].append(
                        {**sample.labels, "value": sample.value}
                    )
                elif sample.name.startswith("litestream_"):
                    db_path = sample.labels.get("db")
                    if db_path is None:
                        continue
                    db = db_name_lookup.get(db_path)
                    if db is None:
                        # Path from metrics may not match resolved path
                        continue
                    metrics_by_db.setdefault(db, {})[sample.name] = sample.value
                elif sample.name in ["go_goroutines", "go_threads"]:
                    go_stats[sample.name] = sample.value

    return Response.html(
        await datasette.render_template(
            "litestream.html",
            context={
                "process": {
                    "pid": litestream_process.process.pid,
                    "status": (
                        "alive" if litestream_process.process.poll() is None else "died"
                    ),
                    "socket": litestream_process.socket_path,
                },
                "daemon_info": daemon_info,
                "managed_databases": managed,
                "socket_error": socket_error,
                "logs": open(litestream_process.logfile.name, "r").read(),
                "metrics_enabled": metrics_enabled,
                "litestream_config": json.dumps(
                    redact_credentials(litestream_process.daemon_config or {}), indent=2
                ),
                "replica_operations": replica_operations,
                "metrics_by_db": metrics_by_db,
                "go_stats": go_stats,
            },
            request=request,
        )
    )
