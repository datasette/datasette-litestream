from datasette.app import Datasette
import pytest
import sqlite_utils
from pathlib import Path
import time

actor_root = {"a": {"id": "root"}}


@pytest.fixture
def students_db_path(tmpdir):
    path = str(tmpdir / "students.db")
    db = sqlite_utils.Database(path)
    db["students"].insert_all(
        [
            {"name": "alex", "age": 10},
            {"name": "brian", "age": 20},
            {"name": "craig", "age": 30, "[weird (column)]": 1},
        ]
    )
    db.execute("create table courses(name text primary key) without rowid")
    db["courses"].insert_all(
        [
            {"name": "MATH 101"},
            {"name": "MATH 102"},
        ]
    )
    return path


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-litestream" in installed_plugins


@pytest.mark.asyncio
async def test_no_litestream_config():
    datasette = Datasette(memory=True)

    response = await datasette.client.get("/-/litestream-status")
    assert response.status_code == 403

    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    assert response.text == "<h1>Litestream not running</h1>"


@pytest.mark.asyncio
async def test_basic_db_level(students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    assert not Path(backup_dir).exists()

    datasette = Datasette(
        [students_db_path],
        metadata={
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            }
        },
    )

    response = await datasette.client.get("/-/litestream-status")
    assert response.status_code == 403

    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    assert "<title>Litestream status</title>" in response.text
    assert (
        "WARNING: metrics-addr was not defined, so no litestream metrics are available."
        in response.text
    )

    # have to wait a second for litestream to write the first replica
    time.sleep(1)
    assert Path(backup_dir).exists()
    assert (Path(backup_dir) / "generations").exists()


@pytest.mark.asyncio
async def test_metrics(students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    assert not Path(backup_dir).exists()

    datasette = Datasette(
        [students_db_path],
        metadata={
            "plugins": {"datasette-litestream": {"metrics-addr": ":9998"}},
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            },
        },
    )

    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )

    assert response.status_code == 200
    assert "<title>Litestream status</title>" in response.text
    assert "<h2>Metrics</h2>" in response.text
