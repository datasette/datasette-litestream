from datasette import hookimpl, Permission, Forbidden
from datasette.utils.asgi import Response
from pathlib import Path
import subprocess
import tempfile
import json
import httpx
import os
import shutil
from prometheus_client.parser import text_string_to_metric_families


def litestream_path():
    # First try to see if litestream was bundled with that package, in a pre-built wheel
    wheel_path = Path(__file__).resolve().parent / "bin" / "litestream"
    if wheel_path.exists():
        return str(wheel_path)

    # Fallback to any litestream binary on the system.
    executable_path = shutil.which("litestream")

    if executable_path is None:
        raise Exception("litestream not found.")

    return str(executable_path)


process = None
litestream_config = None
logfile = tempfile.NamedTemporaryFile(suffix=".log", delete=False)

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
    if action == "litestream-view-status" and actor and actor.get("id") == "root":
        return True

@hookimpl
def menu_links(datasette, actor):
    async def inner():
        if await datasette.permission_allowed(
            actor, "litestream-view-status", default=False
        ) and datasette.plugin_config("datasette-litestream") is not None:
            return [
                {
                    "href": datasette.urls.path("/-/litestream-status"),
                    "label": "Litestream Status",
                },
            ]

    return inner

@hookimpl
def startup(datasette):
    global litestream_config
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

        db_litestream_config = {
            "path": str(db_path.resolve()),
        }
        if plugin_config_db is not None:
            db_litestream_config = {**db_litestream_config, **plugin_config_db}

            # "path": str(db_path.parent.resolve() / f"{db_name}-backup")

        if all_replicate is not None:
            for i, template in enumerate(all_replicate):
                url = template.replace("$DB", db_name).replace("$PWD", os.getcwd())

                if "replicas" in db_litestream_config:
                    db_litestream_config["replicas"].append(
                        {"url": url, "name": f"t{i}"}
                    )
                else:
                    db_litestream_config["replicas"] = [{"url": url, "name": f"t{i}"}]

        litestream_config["dbs"].append(db_litestream_config)
    litestream_replicate(litestream_config)


def litestream_replicate(config):
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        f.write(bytes(json.dumps(config), "utf-8"))
        config_path = Path(f.name)
    global process
    process = subprocess.Popen(
        [litestream_path(), "replicate", "-config", str(config_path)],
        #stdout=subprocess.PIPE,
        stderr=logfile,
    )


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

    global process

    metrics_by_db = {}
    go_stats = {}

    metrics_enabled = litestream_config.get("addr") is not None

    if metrics_enabled:

      # litestream metrics give the absolute path to the database, so create a mapping
      # to the datasette db names
      db_name_lookup = {}
      for db_name, db in datasette.databases.items():
          if db.path is None:
              continue
          db_name_lookup[str(Path(db.path).resolve())] = db_name

      # TODO detect when non-localhost addresses are used
      addr = litestream_config.get("addr")
      metrics = httpx.get(f"http://localhost{addr}/metrics").text

      for family in text_string_to_metric_families(metrics):
          for sample in family.samples:
              # litestream_replica_validation_total has `name` and `status` values that I don't understand
              if (
                  sample.name.startswith("litestream_")
                  and sample.name != "litestream_replica_validation_total"
              ):
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
                    "pid": process.pid,
                    "status": "alive" if process.poll() is None else "died"
                },
                "logs": open(logfile.name, "r").read(),
                "metrics_enabled": metrics_enabled,
                # TODO redact credentials if they are in here :(
                "litestream_config": json.dumps(litestream_config, indent=2),
                "metrics_by_db": metrics_by_db,
                "go_stats": go_stats,
            },
        )
    )
