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


class LitestreamProcess:
    """Manages a litestream subprocess for database replication."""

    # The underlying subprocess.Popen() that gets kicked off
    process = None

    # the litestream.yaml config, as a dict
    litestream_config = None

    # Temporary file where the subprocess logs get forwarded to
    logfile = None

    # Temporary file where the litestream.yaml gets saved to
    configfile = None

    # Hash of current credentials for change detection
    current_credentials_hash = None

    # atexit handler function (stored so we can unregister it)
    _atexit_handler = None

    # Background task for credential refresh (stored to prevent GC)
    _refresh_task = None

    def __init__(self):
        self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=True)

    def start_replicate(self):
        """Starts the litestream process with the given config, logging to logfile."""
        litestream_path = resolve_litestream_path()

        self.configfile = tempfile.NamedTemporaryFile(suffix=".yml", delete=False)

        # Build environment - litestream needs credentials as env vars when using session tokens
        env = os.environ.copy()
        if "session-token" in self.litestream_config:
            # When using session tokens (STS credentials), pass all credentials via environment
            # because litestream doesn't support session-token in config file and prefers
            # config file credentials over env vars.
            # We must use AWS_* vars directly (not LITESTREAM_*) because litestream's
            # applyLitestreamEnv() only copies LITESTREAM_* to AWS_* if AWS_* is not already set.
            # If the user has existing AWS credentials in their environment, they would take
            # precedence and cause "InvalidToken" errors when combined with our session token.
            env["AWS_ACCESS_KEY_ID"] = self.litestream_config["access-key-id"]
            env["AWS_SECRET_ACCESS_KEY"] = self.litestream_config["secret-access-key"]
            env["AWS_SESSION_TOKEN"] = self.litestream_config["session-token"]
            # Write config without credentials - they'll come from env vars
            config_for_file = {
                k: v
                for k, v in self.litestream_config.items()
                if k not in ("access-key-id", "secret-access-key", "session-token")
            }
        else:
            config_for_file = self.litestream_config

        with self.configfile as f:
            f.write(bytes(json.dumps(config_for_file), "utf-8"))
            config_path = Path(f.name)

        self.process = subprocess.Popen(
            [litestream_path, "replicate", "-config", str(config_path)],
            stderr=self.logfile,
            env=env,
        )

        # wait 500ms to see if there are instant errors (typically config typos)
        time.sleep(0.5)
        status = self.process.poll()
        if status is not None:
            logs = open(self.logfile.name, "r").read()
            raise Exception(
                f"datasette-litestream litestream process failed with return code {status}. Logs:"
                + logs
            )

        # Sometimes Popen doesn't die on exit, so explicitly attempt to kill it on process exit
        def onexit():
            if self.process:
                self.process.kill()
            if self.configfile and Path(self.configfile.name).exists():
                Path(self.configfile.name).unlink()

        self._atexit_handler = onexit
        atexit.register(onexit)

    def stop_replicate(self):
        """Gracefully stop the litestream process."""
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

    def update_credentials(self, new_creds: dict):
        """Update credentials in the litestream config."""
        self.litestream_config["access-key-id"] = new_creds["access-key-id"]
        self.litestream_config["secret-access-key"] = new_creds["secret-access-key"]
        if "session-token" in new_creds:
            self.litestream_config["session-token"] = new_creds["session-token"]
        elif "session-token" in self.litestream_config:
            # Remove session token if no longer present in new credentials
            del self.litestream_config["session-token"]
        self.current_credentials_hash = credentials_hash(new_creds)

    def restart_with_new_credentials(self, new_creds: dict):
        """Stop the current process and restart with new credentials."""
        self.stop_replicate()
        self.update_credentials(new_creds)
        self.start_replicate()


# global variable that tracks each datasette-litestream instance. There is usually just 1,
# but in test suites there may be multiple Datasette instances.
# The keys are a UUID generated in the startup hook, values are a LitestreamProcess
processes = {}

# The uuid generated at startup is stored on the datasette object, stored in this key attr.
# Meant so we can retrieve it in the separate litestream_status route
DATASETTE_LITESTREAM_PROCESS_KEY = "__DATASETTE_LITESTREAM_PROCESS_KEY__"


def resolve_litestream_path():
    """resolives the full path to a litestream binary. Hopefully is bundled in the installed wheel"""

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
        )
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
                    "href": datasette.urls.path("/-/litestream-status"),
                    "label": "Litestream Status",
                },
            ]

    return inner


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
                litestream_process.restart_with_new_credentials(new_creds)

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
    litestream_process.litestream_config = {"dbs": []}

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
    if uses_dynamic_credentials:
        try:
            dynamic_creds = get_dynamic_credentials(plugin_config_top)
            litestream_process.litestream_config["access-key-id"] = dynamic_creds[
                "access-key-id"
            ]
            litestream_process.litestream_config["secret-access-key"] = dynamic_creds[
                "secret-access-key"
            ]
            if "session-token" in dynamic_creds:
                litestream_process.litestream_config["session-token"] = dynamic_creds[
                    "session-token"
                ]
            litestream_process.current_credentials_hash = credentials_hash(
                dynamic_creds
            )
        except Exception as e:
            raise StartupError(
                f"datasette-litestream: failed to load initial credentials: {e}"
            ) from e
    else:
        # Use static credentials from config
        if "access-key-id" in plugin_config_top:
            litestream_process.litestream_config["access-key-id"] = (
                plugin_config_top.get("access-key-id")
            )

        if "secret-access-key" in plugin_config_top:
            litestream_process.litestream_config["secret-access-key"] = (
                plugin_config_top.get("secret-access-key")
            )

        if "session-token" in plugin_config_top:
            litestream_process.litestream_config["session-token"] = (
                plugin_config_top.get("session-token")
            )

    if "metrics-addr" in plugin_config_top:
        litestream_process.litestream_config["addr"] = plugin_config_top.get(
            "metrics-addr"
        )

    all_replicate = plugin_config_top.get("all-replicate")

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

        db_litestream_config = {
            "path": str(db_path.resolve()),
        }
        if plugin_config_db is not None:
            # TODO restrict the possible keys here. We don't want plugins to redefine "replicas" or "path"
            db_litestream_config = {**db_litestream_config, **plugin_config_db}

        if all_replicate is not None:
            for i, template in enumerate(all_replicate):
                url = (
                    template.replace("$DB_NAME", db_name)
                    .replace("$DB_DIRECTORY", str(db_path.resolve().parent))
                    .replace("$PWD", os.getcwd())
                )

                if "replicas" in db_litestream_config:
                    db_litestream_config["replicas"].append(
                        {"url": url, "name": f"t{i}"}
                    )
                else:
                    db_litestream_config["replicas"] = [{"url": url, "name": f"t{i}"}]

        litestream_process.litestream_config["dbs"].append(db_litestream_config)

    # don't run litestream if no top-level or db-level datasette-litestream config was given
    if not plugin_config_top and len(litestream_process.litestream_config["dbs"]) == 0:
        return

    startup_id = str(uuid.uuid4())
    processes[startup_id] = litestream_process
    setattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, startup_id)

    litestream_process.start_replicate()

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
        (r"^/-/litestream-status$", litestream_status),
    ]


async def litestream_status(scope, receive, datasette, request):
    if not await datasette.allowed(
        actor=request.actor, action="litestream-view-status"
    ):
        raise Forbidden("Permission denied for litestream-view-status")

    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, None)

    if startup_id is None:
        return Response.html("<h1>Litestream not running</h1>")

    global proccesses
    litestream_process = processes.get(startup_id)

    if litestream_process is None:
        return Response.html("<h1>Litestream not running</h1>")

    replica_operations = {
        "bytes": [],
        "total": [],
    }
    metrics_by_db = {}
    go_stats = {}

    metrics_enabled = litestream_process.litestream_config.get("addr") is not None

    if metrics_enabled:
        # litestream metrics give the absolute path to the database, so create a mapping
        # to the datasette db names
        db_name_lookup = {}
        for db_name, db in datasette.databases.items():
            if db.path is None:
                continue
            db_name_lookup[str(Path(db.path).resolve())] = db_name

        # TODO detect when non-localhost addresses are used
        addr = litestream_process.litestream_config.get("addr")
        metrics_page = httpx.get(f"http://localhost{addr}/metrics").text

        for family in text_string_to_metric_families(metrics_page):
            for sample in family.samples:
                # TODO also  ???
                if sample.name == "litestream_replica_operation_bytes_total":
                    replica_operations["bytes"].append(
                        {
                            **sample.labels,
                            "value": sample.value,
                        }
                    )
                elif sample.name == "litestream_replica_operation_total":
                    replica_operations["total"].append(
                        {
                            **sample.labels,
                            "value": sample.value,
                        }
                    )

                elif (
                    sample.name.startswith("litestream_")
                    # litestream_replica_validation_total has `name` and `status` values that I don't understand
                    and sample.name != "litestream_replica_validation_total"
                ):
                    db_path = sample.labels.get("db")
                    db = db_name_lookup.get(db_path)
                    if db is None:
                        # Path from metrics may not match resolved path (e.g. /tmp vs /private/tmp)
                        continue

                    if metrics_by_db.get(db) is None:
                        metrics_by_db[db] = {}

                    metrics_by_db[db][sample.name] = sample.value
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
                },
                "logs": open(litestream_process.logfile.name, "r").read(),
                "metrics_enabled": metrics_enabled,
                "litestream_config": json.dumps(
                    redact_credentials(litestream_process.litestream_config), indent=2
                ),
                "replica_operations": replica_operations,
                "metrics_by_db": metrics_by_db,
                "go_stats": go_stats,
            },
        )
    )
