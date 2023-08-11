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
from prometheus_client.parser import text_string_to_metric_families


class LitestreamProcess:
    """ """

    # The underyling subprocess.Popen() that gets kicked off
    process = None

    # the litestream.yaml config, as a dict
    litestream_config = None

    # Temporary file where the subprocess logs get forwarded too
    logfile = None

    # Temporary file where the litestream.yaml gets saved to
    configfile = None

    def __init__(self):
        self.logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=True)

    def start_replicate(self):
        """starts the litestream process with the given config, logging to logfile"""

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
            raise Exception(
                f"datasette-litestream litestream process failed with return code {status}. Logs:"
                + logs
            )

        # Sometimes Popen doesn't die on exit, so explicitly attempt to kill it on proccess exit
        def onexit():
            self.process.kill()
            Path(self.configfile.name).unlink()

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


@hookimpl
def startup(datasette):
    global processes

    litestream_process = LitestreamProcess()
    litestream_process.litestream_config = {"dbs": []}

    plugin_config_top = datasette.plugin_config("datasette-litestream") or {}

    if "access-key-id" in plugin_config_top:
        litestream_process.litestream_config["access-key-id"] = plugin_config_top.get(
            "access-key-id"
        )

    if "secret-access-key" in plugin_config_top:
        litestream_process.litestream_config[
            "secret-access-key"
        ] = plugin_config_top.get("secret-access-key")

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

        # skip this DB is "all-replicate" was not defined or no db-level config was given
        if plugin_config_db is None and all_replicate is None:
            continue

        db_litestream_config = {
            "path": str(db_path.resolve()),
        }
        if plugin_config_db is not None:
            # TODO restrict the possible keys here. We don't want to plugins to redefine "replicas" or "path"
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
                    replica_operations["bytes"].append({
                      **sample.labels,
                      "value": sample.value,
                    })
                elif sample.name == "litestream_replica_operation_total":
                    replica_operations["total"].append({
                      **sample.labels,
                      "value": sample.value,
                    })

                elif (
                    sample.name.startswith("litestream_")
                    # litestream_replica_validation_total has `name` and `status` values that I don't understand
                    and sample.name != "litestream_replica_validation_total"
                ):
                    print(sample.name)
                    db = db_name_lookup[sample.labels.get("db")]

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
                    if litestream_process.process.poll() is None
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
