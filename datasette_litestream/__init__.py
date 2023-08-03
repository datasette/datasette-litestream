from datasette import hookimpl
from pathlib import Path
import subprocess
import tempfile
import json


def litestream_replicate(config):
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        f.write(bytes(json.dumps(config), "utf-8"))
        config_path = Path(f.name)

    process = subprocess.Popen(
        ["litestream", "replicate", "-config", str(config_path)],
    )
    print(process.pid)


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

        litestream_config["dbs"].append(db_litestream_config)

    litestream_replicate(litestream_config)
