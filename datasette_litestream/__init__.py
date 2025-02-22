import sys
from datasette import hookimpl, Permission, Forbidden
from datasette.utils.asgi import Response
from pathlib import Path
import atexit
import httpx
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import threading
from prometheus_client.parser import text_string_to_metric_families


class LitestreamProcess:
    """ """

    # The underyling subprocess.Popen() that gets kicked off
    process = None

    def __init__(self, litestream_config):
        """Initialize the LitestreamProcess with a configuration.

        Args:
            litestream_config (dict): The litestream.yaml configuration as a dictionary
        """
        self.process = None
        self.litestream_config = litestream_config
        self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=True)
        self.configfile = None
        self._register_cleanup()

    def start_replicate(self):
        """Starts the litestream process with the current configuration.

        Raises:
            Exception: If the process fails to start or encounters immediate errors
        """
        if self.process is not None:
            raise RuntimeError("Litestream process is already running")

        litestream_path = resolve_litestream_path()

        self.configfile = tempfile.NamedTemporaryFile(suffix=".yml", delete=False)

        with self.configfile as f:
            f.write(bytes(json.dumps(self.litestream_config), "utf-8"))
            config_path = Path(f.name)

        self.process = subprocess.Popen(
            [litestream_path, "replicate", "-config", str(config_path)],
            stderr=self.logfile,
        )

        # wait 500ms to see if there are instant errors (typically config typos)
        time.sleep(0.5)
        status = self.process.poll()
        if status is not None:
            logs = open(self.logfile.name, "r").read()
            self._cleanup_files()
            raise Exception(
                f"datasette-litestream litestream process failed with return code {status}. Logs:"
                + logs
            )

    def stop_replicate(self):
        """Stops the currently running litestream process and cleans up temporary files."""
        if self.process is not None:
            self.process.kill()
            self.process.wait()  # Wait for the process to fully terminate
            self.process = None
            self._cleanup_files()

    def restart_replicate(self, litestream_config):
        """Restarts the litestream process with new configuration.

        Args:
            litestream_config (dict): The new litestream.yaml configuration as a dictionary
        """
        self.stop_replicate()
        self.litestream_config = litestream_config
        self.start_replicate()

    def _cleanup_files(self):
        """Clean up temporary configuration file."""
        if self.configfile is not None:
            try:
                Path(self.configfile.name).unlink()
                self.configfile = None
            except FileNotFoundError:
                pass

    def _register_cleanup(self):
        """Register cleanup handlers for process exit."""

        def onexit():
            if self.process is not None:
                self.process.kill()
            self._cleanup_files()

        atexit.register(onexit)


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
def register_permissions(datasette):
    return [
        Permission(
            name="litestream-view-status",
            abbr=None,
            description="View litestream statistics and status updates.",
            takes_database=False,
            takes_resource=False,
            default=False,
        )
    ]


@hookimpl
def permission_allowed(actor, action):
    # TODO only root can see it?
    if action == "litestream-view-status" and actor and actor.get("id") == "root":
        return True


@hookimpl
def menu_links(datasette, actor):
    async def inner():
        if (
            await datasette.permission_allowed(
                actor, "litestream-view-status", default=False
            )
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


def build_litestream_config(datasette):
    litestream_config = {"dbs": []}
    plugin_config_top = datasette.plugin_config("datasette-litestream") or {}

    if "access-key-id" in plugin_config_top:
        litestream_config["access-key-id"] = plugin_config_top.get("access-key-id")
    if "secret-access-key" in plugin_config_top:
        litestream_config["secret-access-key"] = plugin_config_top.get(
            "secret-access-key"
        )
    if "metrics-addr" in plugin_config_top:
        litestream_config["addr"] = plugin_config_top.get("metrics-addr")

    all_replicate = plugin_config_top.get("all-replicate")

    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        db_path = Path(db.path)
        plugin_config_db = datasette.plugin_config(
            "datasette-litestream", db_name, fallback=False
        )
        # Skip this DB if neither a DB-level config nor "all-replicate" is defined.
        if plugin_config_db is None and all_replicate is None:
            continue
        db_litestream_config = {"path": str(db_path.resolve())}
        if plugin_config_db is not None:
            # TODO restrict the possible keys here.
            db_litestream_config = {**db_litestream_config, **plugin_config_db}
        if all_replicate:
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
        litestream_config["dbs"].append(db_litestream_config)

    if not plugin_config_top and len(litestream_config["dbs"]) == 0:
        return
    return litestream_config


def run_litestream_thread(datasette):
    """
    Runs the LitestreamProcess in a separate thread.
    This thread starts the Litestream replication process and,
    if the 'all-replicate' configuration option is set,
    continuously monitors datasette.databases for changes once every second.
    """
    litestream_config = build_litestream_config(datasette)
    if litestream_config is None:
        print("litestream_config is None")
        return

    litestream_process = LitestreamProcess(litestream_config)
    startup_id = str(uuid.uuid4())
    processes[startup_id] = litestream_process
    setattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, startup_id)

    try:
        litestream_process.start_replicate()
    except Exception as e:
        print(f"Error starting litestream: {e}", file=sys.stderr)
        return

    # If the 'all-replicate' option is enabled, monitor for changes.
    plugin_config_top = datasette.plugin_config("datasette-litestream") or {}
    if plugin_config_top.get("all-replicate"):
        last_database_keys = tuple(sorted(datasette.databases.keys()))
        while True:
            try:
                time.sleep(1)
                new_database_keys = tuple(sorted(datasette.databases.keys()))
                if new_database_keys != last_database_keys:
                    print(
                        "Database keys changed! New keys: {}. Restarting Litestream replication.".format(
                            set(new_database_keys).difference(last_database_keys)
                        )
                    )
                    last_database_keys = new_database_keys
                    new_config = build_litestream_config(datasette)
                    litestream_process.restart_replicate(new_config)
            except Exception as ex:
                print(f"Error in monitor_databases thread: {ex}", file=sys.stderr)
    else:
        # If no monitoring is required, keep the thread alive.
        while True:
            time.sleep(60)


@hookimpl
def startup(datasette):
    thread = threading.Thread(
        target=run_litestream_thread, args=(datasette,), daemon=True
    )
    thread.start()


@hookimpl
def register_routes():
    return [
        (r"^/-/litestream-status$", litestream_status),
    ]


async def litestream_status(scope, receive, datasette, request):
    if not await datasette.permission_allowed(
        request.actor, "litestream-view-status", default=False
    ):
        raise Forbidden("Permission denied for litestream-view-status")

    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, None)
    if startup_id is None:
        return Response.html("<h1>Litestream not running</h1>")

    litestream_process = processes.get(startup_id)
    if litestream_process is None:
        return Response.html("<h1>Litestream not running</h1>")

    replica_operations = {"bytes": [], "total": []}
    metrics_by_db = {}
    go_stats = {}

    metrics_enabled = litestream_process.litestream_config.get("addr") is not None
    if metrics_enabled:
        # Create a mapping of database file paths to Datasette DB names.
        db_name_lookup = {}
        for db_name, db in datasette.databases.items():
            if db.path is None:
                continue
            db_name_lookup[str(Path(db.path).resolve())] = db_name

        addr = litestream_process.litestream_config.get("addr")
        metrics_page = httpx.get(f"http://localhost{addr}/metrics").text
        for family in text_string_to_metric_families(metrics_page):
            for sample in family.samples:
                if sample.name == "litestream_replica_operation_bytes_total":
                    replica_operations["bytes"].append(
                        {**sample.labels, "value": sample.value}
                    )
                elif sample.name == "litestream_replica_operation_total":
                    replica_operations["total"].append(
                        {**sample.labels, "value": sample.value}
                    )
                elif (
                    sample.name.startswith("litestream_")
                    and sample.name != "litestream_replica_validation_total"
                ):
                    db = db_name_lookup.get(sample.labels.get("db"), "unknown")
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
                    "status": "alive"
                    if (litestream_process and litestream_process.process.poll() is None)
                    else "died",
                },
                "logs": open(litestream_process.logfile.name, "r").read(),
                "metrics_enabled": metrics_enabled,
                # TODO redact credentials if they are in here :(
                "litestream_config": json.dumps(
                    litestream_process.litestream_config, indent=2
                ),
                "replica_operations": replica_operations,
                "metrics_by_db": metrics_by_db,
                "go_stats": go_stats,
            },
        )
    )
