from datasette import hookimpl
from datasette.utils.asgi import Response
from pathlib import Path
import subprocess
import tempfile
import json
import httpx
from litestream import litestream

process = None

@hookimpl
def startup(datasette):
    litestream_config = {"dbs": []}

    plugin_config_top = datasette.plugin_config("datasette-litestream")

    if "access-key-id" in plugin_config_top:
        litestream_config["access-key-id"] = plugin_config_top.get("access-key-id")

    if "secret-access-key" in plugin_config_top:
        litestream_config["secret-access-key"] = plugin_config_top.get(
            "secret-access-key"
        )

    if "addr" in plugin_config_top:
        litestream_config["addr"] = plugin_config_top.get("addr")

    all_replicate = plugin_config_top.get("all-replicate")

    for db_name, db in datasette.databases.items():
        print(
            f"db_name={db_name} is_mutable={db.is_mutable} is_memory={db.is_memory} path={db.path}"
        )

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
                url = template.replace("$DB", db_name)

                if "replicas" in db_litestream_config:
                    db_litestream_config["replicas"].append(
                        {
                            "url": url,
                            "name": f"t{i}"
                        }
                    )
                else:
                    db_litestream_config["replicas"] = [{"url": url, "name": f"t{i}"}]

        litestream_config["dbs"].append(db_litestream_config)
    print(litestream_config)
    litestream_replicate(litestream_config)

def litestream_replicate(config):
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        f.write(bytes(json.dumps(config), "utf-8"))
        config_path = Path(f.name)

    global process
    process = subprocess.Popen(
        [litestream.bin_path(), "replicate", "-config", str(config_path)],
        stdout=subprocess.PIPE
    )
    print(process.pid)

@hookimpl
def register_routes():
    return [
        (r"^/-/litestream-status$", litestream_status),
    ]
async def litestream_status(scope, receive, datasette, request):
    metrics_by_db = {}
    metrics = httpx.get("http://localhost:9090/metrics").text
    from prometheus_client.parser import text_string_to_metric_families
    for family in text_string_to_metric_families(metrics):
      for sample in family.samples:
        print(sample)
        if sample.name.startswith("litestream_"):
            if metrics_by_db.get(sample.labels.get('db')) is None:
                metrics_by_db[sample.labels.get('db')] = {}
            metrics_by_db[sample.labels.get('db')][sample.name] = sample.value

    return Response.json(metrics_by_db)
