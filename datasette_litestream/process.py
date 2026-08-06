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
import time
from pathlib import Path

from datasette.utils import StartupError
from pydantic import ValidationError

from ._client import LitestreamClient
from .config import Credentials, LitestreamConfig, LoadedCredentials


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
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=30, check=False
    )
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
    if config.credentials_file:
        return load_credentials_from_file(config.credentials_file)
    elif config.credentials_command:
        return load_credentials_from_command(config.credentials_command)
    return None


def credentials_hash(creds: Credentials | None) -> str:
    """Return a hash string for comparing credentials."""
    if creds is None:
        return ""
    return json.dumps(
        creds.model_dump(by_alias=True, exclude_none=True), sort_keys=True
    )


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
        # Credentials (a config.Credentials or None) and a hash for change
        # detection.
        self.credentials = None
        self.current_credentials_hash = None
        # path (str) -> replica_url for every database we have registered.
        self.registered = {}
        # Startup warnings (e.g. in-memory databases), surfaced on the admin page.
        self.warnings = []
        # Temp files. The logfile handle stays open for the daemon's lifetime:
        # it is passed to Popen as stderr.
        self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=True)  # noqa: SIM115
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

        # Wait briefly to catch instant failures (typically config typos).
        time.sleep(0.5)
        status = self.process.poll()
        if status is not None:
            logs = Path(self.logfile.name).read_text()
            raise RuntimeError(
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

    def _require_client(self) -> "LitestreamClient":
        """Return the control socket client, or raise if the daemon is down."""
        if self.client is None:
            raise RuntimeError(
                "datasette-litestream: the litestream daemon is not running"
            )
        return self.client

    def register_db(self, db_path: str, replica_url: str) -> dict:
        """Register a database for replication over the control socket."""
        result = self._require_client().register(db_path, replica_url)
        self.registered[str(db_path)] = replica_url
        return result

    def unregister_db(self, db_path: str, timeout=None) -> dict:
        """Unregister a database; the daemon performs a final sync first."""
        result = self._require_client().unregister(db_path, timeout=timeout)
        self.registered.pop(str(db_path), None)
        return result

    def reregister_all(self):
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
