"""The litestream daemon: lifecycle, credentials and the process registry.

``LitestreamProcess`` owns a long-lived ``litestream replicate`` subprocess
and its control socket; the module-level ``processes`` dict maps the startup
UUID stored on each Datasette instance to its process so route handlers can
find it via ``get_process()``.
"""

import atexit
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
from datasette.utils import StartupError
from pydantic import ValidationError

from ._client import LitestreamClient
from .config import Credentials, LitestreamConfig, LoadedCredentials, LoggingConfig


def load_credentials_from_file(path: str) -> Credentials:
    """Load credentials from a JSON file."""
    with open(path) as f:
        data = json.load(f)
    try:
        return LoadedCredentials.model_validate(data)
    except ValidationError as e:
        raise StartupError(
            f"Credentials file {path} must contain 'access-key-id' and "
            f"'secret-access-key': {e}"
        ) from e


def load_credentials_from_command(command: str) -> Credentials:
    """Execute a command and parse its JSON output for credentials."""
    args = shlex.split(command)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise StartupError(
            f"Credentials command timed out after {e.timeout} seconds"
        ) from e
    if result.returncode != 0:
        raise StartupError(
            f"Credentials command failed with return code {result.returncode}: {result.stderr}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise StartupError(f"Credentials command output is not valid JSON: {e}")
    try:
        return LoadedCredentials.model_validate(data)
    except ValidationError as e:
        raise StartupError(
            "Credentials command output must contain 'access-key-id' and "
            f"'secret-access-key': {e}"
        ) from e


def get_dynamic_credentials(config: LitestreamConfig) -> Credentials | None:
    """Get credentials from file or command if configured."""
    if config.credentials.file:
        return load_credentials_from_file(config.credentials.file)
    elif config.credentials.command:
        return load_credentials_from_command(config.credentials.command)
    return None


def credentials_hash(creds: Credentials | None) -> str:
    """Return a hash string for comparing credentials."""
    if creds is None:
        return ""
    return json.dumps(
        creds.model_dump(by_alias=True, exclude_none=True), sort_keys=True
    )


def credentials_env(creds: Credentials | None) -> dict:
    """Translate credentials into AWS_* environment variables.

    litestream >= 0.5 picks up S3 credentials from the daemon's environment when
    a database is registered with an ``s3://`` replica URL, so we always pass
    credentials this way rather than embedding them in the config file. We set
    the AWS_* names directly (not LITESTREAM_*) so they take effect regardless of
    litestream's env-precedence rules, and so session tokens work consistently.
    """
    if creds is None:
        return {}
    env = {}
    if creds.access_key_id is not None:
        env["AWS_ACCESS_KEY_ID"] = creds.access_key_id
    if creds.secret_access_key is not None:
        env["AWS_SECRET_ACCESS_KEY"] = creds.secret_access_key
    if creds.session_token is not None:
        env["AWS_SESSION_TOKEN"] = creds.session_token
    return env


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
        raise RuntimeError("litestream not found.")

    return str(executable_path)


class LitestreamProcess:
    """Manages a long-lived ``litestream replicate`` daemon.

    The daemon is started once with its control socket enabled and no databases
    in its config file. Databases are added and removed at runtime over the
    control socket (register/unregister), so no daemon restart is needed when the
    set of replicated databases changes.
    """

    def __init__(self, logging_config: LoggingConfig | None = None):
        self.logging = logging_config or LoggingConfig()
        self.process = None
        # Control socket path and a client bound to it.
        self.socket_dir = None
        self.socket_path = None
        self.client = None
        # The daemon config (dict) we wrote out.
        self.daemon_config = None
        # Metrics/pprof bind address, if configured.
        self.metrics_addr = None
        # Credentials (a config.Credentials or None) and a hash for change
        # detection.
        self.credentials = None
        self.current_credentials_hash = None
        # path (str) -> replica_url for every database we have registered.
        self.registered = {}
        # Datasette database name -> path, remembered at registration time so
        # the manage API can still resolve a database that was later detached
        # from Datasette.
        self.registered_names = {}
        # Startup warnings (e.g. in-memory databases), surfaced on the admin page.
        self.warnings = []
        # The logfile receives the daemon's stderr (litestream is configured
        # with ``logging.stderr: true``, so all its log output lands here).
        # The handle stays open for the daemon's lifetime: it is passed to
        # Popen as stderr.
        if self.logging.path:
            try:
                # Created 0600: the daemon's stderr carries replica URLs and
                # request detail that other local users shouldn't read by
                # default. A pre-existing file keeps its permissions.
                fd = os.open(
                    self.logging.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                self.logfile = os.fdopen(fd, "ab")
            except OSError as e:
                raise StartupError(
                    f"datasette-litestream: cannot open log file "
                    f"{self.logging.path!r}: {e}"
                ) from e
        else:
            self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=True)  # noqa: SIM115
        self.configfile = None
        # Key of this process in the module-level ``processes`` registry,
        # set at registration; used to prune the entry on final teardown.
        self.startup_id = None
        # atexit handler (stored so we can unregister it) and refresh task.
        self._atexit_handler = None
        self._refresh_task = None
        # Serializes daemon lifecycle changes and runtime (un)registration,
        # so a credential-rotation restart cannot race the manage API and
        # silently drop entries from ``registered``. A plain Lock (not RLock):
        # the ``*_locked`` helpers must never call the public methods.
        self._lock = threading.Lock()

    # --- Daemon lifecycle -------------------------------------------------

    def start_daemon(self, socket_timeout: float = 5.0):
        """Start the litestream daemon with the control socket enabled."""
        with self._lock:
            self._start_daemon_locked(socket_timeout=socket_timeout)

    def _start_daemon_locked(self, socket_timeout: float = 5.0):
        litestream_path = resolve_litestream_path()

        self.socket_dir = tempfile.mkdtemp(prefix="datasette-litestream-")
        self.socket_path = str(Path(self.socket_dir) / "litestream.sock")

        self.daemon_config = {
            "socket": {
                "enabled": True,
                "path": self.socket_path,
                "permissions": 0o600,
            },
            "logging": {
                "level": self.logging.level,
                "type": self.logging.type,
                # litestream logs to stdout by default, which would bypass the
                # Popen stderr redirect and spam the Datasette console.
                "stderr": True,
            },
        }
        if self.metrics_addr:
            self.daemon_config["addr"] = self.metrics_addr

        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            f.write(bytes(json.dumps(self.daemon_config), "utf-8"))
            config_path = Path(f.name)
        self.configfile = f

        env = os.environ.copy()
        env.update(credentials_env(self.credentials))

        self.process = subprocess.Popen(
            [litestream_path, "replicate", "-config", str(config_path)],
            stderr=self.logfile,
            env=env,
        )

        # Stop the daemon when the interpreter exits (Datasette has no plugin
        # shutdown hook as of 1.0a32, so atexit is the only exit-time hook).
        # Registered immediately after Popen so the child is covered even if
        # the parent dies while we are still waiting for it to come up.
        self._atexit_handler = self._on_interpreter_exit
        atexit.register(self._atexit_handler)

        try:
            # Wait briefly to catch instant failures (typically config typos).
            time.sleep(0.5)
            status = self.process.poll()
            if status is not None:
                logs = Path(self.logfile.name).read_text()
                raise RuntimeError(
                    f"datasette-litestream litestream process failed with return code {status}. Logs:"
                    + logs
                )

            self._wait_for_socket(timeout=socket_timeout)
        except BaseException:
            # Never leave an orphaned daemon (which holds credentials in its
            # environment) or temp-file litter behind a failed start.
            self._stop_daemon_locked()
            raise

        self.client = LitestreamClient(self.socket_path)

    def _wait_for_socket(self, timeout=5.0):
        """Block until the control socket file appears (or the process dies)."""
        # Only called from start_daemon, after these are set.
        if self.socket_path is None or self.process is None:
            raise RuntimeError(
                "datasette-litestream: _wait_for_socket called before start_daemon"
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(self.socket_path).exists():
                return
            if self.process.poll() is not None:
                logs = Path(self.logfile.name).read_text()
                raise RuntimeError(
                    "datasette-litestream litestream process exited before opening "
                    "its control socket. Logs:" + logs
                )
            time.sleep(0.05)
        raise RuntimeError(
            f"datasette-litestream: control socket {self.socket_path} did not appear "
            f"within {timeout}s"
        )

    def _on_interpreter_exit(
        self, lock_timeout: float = 5.0, wait_timeout: float = 5.0
    ):
        """atexit handler: stop the daemon gracefully at interpreter exit.

        litestream traps SIGTERM and performs a final sync of every database
        to its replica before exiting, so a graceful stop ships the WAL
        writes that landed since the last sync interval — a plain SIGKILL
        would lose them until the next daemon start against the same disk.
        A shorter wait than stop_daemon's default so a wedged daemon cannot
        hang interpreter exit for long.
        """
        task = self._refresh_task
        if task is not None:
            try:
                task.cancel()
            except Exception:  # noqa: BLE001, S110 -- best effort at exit
                pass
        if self._lock.acquire(timeout=lock_timeout):
            try:
                self._stop_daemon_locked(wait_timeout=wait_timeout)
            finally:
                self._lock.release()
            self._release()
        else:
            # The lock is held by an operation that will never finish (we are
            # exiting); fall back to a hard kill so no orphan survives.
            if self.process:
                self.process.kill()
            if self.configfile and Path(self.configfile.name).exists():
                Path(self.configfile.name).unlink(missing_ok=True)

    def stop_daemon(self):
        """Gracefully stop the litestream daemon and clean up temp files.

        This is final teardown: it also closes the log handle and drops the
        process from the registry. A rotation restart goes through
        ``_stop_daemon_locked`` directly, which leaves both intact (the new
        daemon reuses the logfile).
        """
        with self._lock:
            self._stop_daemon_locked()
        self._release()

    def _release(self):
        """Close the log handle and prune the registry entry (idempotent)."""
        if not self.logfile.closed:
            self.logfile.close()
        if self.startup_id is not None:
            processes.pop(self.startup_id, None)
            self.startup_id = None

    def _stop_daemon_locked(self, wait_timeout: float = 10.0):
        if self._atexit_handler:
            atexit.unregister(self._atexit_handler)
            self._atexit_handler = None

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=wait_timeout)
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

    def _require_client(self) -> "LitestreamClient":
        """Return the control socket client, or raise if the daemon is down."""
        if self.client is None:
            raise RuntimeError(
                "datasette-litestream: the litestream daemon is not running"
            )
        return self.client

    def register_db(
        self, db_path: str, replica_url: str, name: str | None = None
    ) -> dict:
        """Register a database for replication over the control socket."""
        with self._lock:
            result = self._require_client().register(db_path, replica_url)
            if result.get("status") != "already_registered":
                self.registered[str(db_path)] = replica_url
            # already_registered: the daemon kept its existing replica URL, so
            # recording the caller's would misreport what is replicating where
            # — and a later rotation restart would then actually switch the
            # destination. The map keeps (or lacks) the URL the daemon uses.
            if name is not None:
                self.registered_names[name] = str(db_path)
            return result

    def unregister_db(self, db_path: str, timeout=None) -> dict:
        """Unregister a database; the daemon performs a final sync first."""
        with self._lock:
            try:
                result = self._require_client().unregister(db_path, timeout=timeout)
            except httpx.ReadTimeout:
                # The daemon received the request and completes the final
                # sync + unregister on its own schedule; only the response
                # outlived our socket read. Prune the maps anyway so a later
                # rotation restart cannot silently re-register a database the
                # operator removed. (Connect/write timeouts are not caught:
                # there the daemon never took the request, so its state — and
                # our map — are unchanged.)
                self._forget_db_locked(db_path)
                raise
            self._forget_db_locked(db_path)
            return result

    def _forget_db_locked(self, db_path) -> None:
        self.registered.pop(str(db_path), None)
        self.registered_names = {
            n: p for n, p in self.registered_names.items() if p != str(db_path)
        }

    def _reregister_all_locked(self):
        """Re-register every known database (used after a daemon restart)."""
        client = self._require_client()
        for db_path, replica_url in list(self.registered.items()):
            client.register(db_path, replica_url)

    # --- Credential rotation ---------------------------------------------

    def update_credentials(self, new_creds: Credentials):
        self.credentials = new_creds
        self.current_credentials_hash = credentials_hash(new_creds)

    def restart_with_new_credentials(self, new_creds: Credentials):
        """Restart the daemon with new credentials and re-register databases.

        Credentials reach litestream through the daemon's environment, which a
        running process can't change, so a rotation requires a restart. We
        preserve the set of replicated databases by re-registering them.

        The lock is held for the whole stop→start→re-register sequence, so a
        concurrent register/unregister blocks until the new daemon is live and
        then lands on it, instead of racing the snapshot and being lost.
        """
        with self._lock:
            registered = dict(self.registered)
            self._stop_daemon_locked()
            self.update_credentials(new_creds)
            self._start_daemon_locked()
            self.registered = registered
            self._reregister_all_locked()


# global variable that tracks each datasette-litestream instance. There is usually just 1,
# but in test suites there may be multiple Datasette instances.
# The keys are a UUID generated in the startup hook, values are a LitestreamProcess
processes = {}

# The uuid generated at startup is stored on the datasette object, stored in this key attr.
# Meant so we can retrieve it in the route handlers
DATASETTE_LITESTREAM_PROCESS_KEY = "__DATASETTE_LITESTREAM_PROCESS_KEY__"


def get_process(datasette):
    """Return the LitestreamProcess for this Datasette instance, or None."""
    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, None)
    if startup_id is None:
        return None
    return processes.get(startup_id)
