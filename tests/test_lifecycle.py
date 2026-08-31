"""Lifecycle integration: the supervised health task and the shutdown() hook.

The health loop is registered with datasette.add_background_task() (so core
launches it after every startup hook, keeps the reference, and shows it at
/-/tasks), and the shutdown() hook tears the daemon down gracefully before
core cancels background tasks and closes databases.
"""

import pytest
from conftest import table
from datasette.app import Datasette

from datasette_litestream.process import get_process


def _file_replica_config(backups_dir):
    return {
        "plugins": {
            "datasette-litestream": {
                "replica-url-template": "file://" + str(backups_dir) + "/$DB_NAME"
            }
        }
    }


@pytest.mark.asyncio
async def test_shutdown_hook_stops_daemon_and_cancels_health_task(
    litestream_binary, tmpdir
):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    datasette = Datasette([db_path], config=_file_replica_config(tmpdir / "backups"))
    await datasette.start_background_tasks()

    litestream_process = get_process(datasette)
    assert litestream_process is not None
    daemon = litestream_process.process
    assert daemon is not None and daemon.poll() is None
    handle = litestream_process._health_handle
    assert handle is not None and handle.state == "running"

    await datasette.invoke_shutdown()

    # The hook stopped the daemon (SIGTERM -> final sync -> exit) and pruned
    # the registry; core then cancelled the health task during its grace wait.
    assert daemon.poll() is not None
    assert litestream_process.process is None
    assert get_process(datasette) is None
    assert handle.state == "cancelled"

    # Idempotent: a second shutdown (e.g. a duplicate lifespan.shutdown
    # message) must not raise.
    await datasette.invoke_shutdown()


@pytest.mark.asyncio
async def test_shutdown_hook_is_noop_without_a_daemon(tmpdir):
    """No datasette-litestream config -> no daemon -> shutdown() no-ops."""
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    datasette = Datasette([db_path])
    await datasette.start_background_tasks()
    assert get_process(datasette) is None
    await datasette.invoke_shutdown()


@pytest.mark.asyncio
async def test_health_task_listed_in_tasks_endpoint(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    datasette = Datasette([db_path], config=_file_replica_config(tmpdir / "backups"))
    datasette.root_enabled = True

    response = await datasette.client.get("/-/tasks.json", actor={"id": "root"})
    assert response.status_code == 200
    data = response.json()
    assert data["launched"] is True
    by_name = {t["name"]: t for t in data["tasks"]}
    health = by_name["datasette-litestream-health"]
    assert health["state"] == "running"
    # The entry-point name from pyproject's [project.entry-points.datasette],
    # via core's best-effort plugin attribution.
    assert health["plugin"] == "litestream"
    assert health["exception"] is None
