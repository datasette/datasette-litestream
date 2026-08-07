"""Tests for the Svelte/Vite admin UI: the page route, the JSON status API,
and the sync/start/stop endpoints (all gated by litestream permissions)."""

import pytest
from conftest import table
from datasette.app import Datasette
from datasette.database import Database

from datasette_litestream.process import get_process

actor_root = {"a": {"id": "root"}}


def _datasette(tmpdir, db_paths):
    backups = tmpdir / "backups"
    ds = Datasette(
        [str(p) for p in db_paths],
        config={
            "plugins": {
                "datasette-litestream": {
                    "replica-url-template": "file://" + str(backups) + "/$DB_NAME"
                }
            }
        },
    )
    ds.root_enabled = True
    return ds, backups


async def root_token(datasette):
    token = await datasette.create_token("root")
    return {"Authorization": f"Bearer {token}"}


def root_cookies(datasette):
    return {"ds_actor": datasette.sign(actor_root, "actor")}


# --- admin page -------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_page_requires_permission(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds, _ = _datasette(tmpdir, [db])

    response = await ds.client.get("/-/litestream")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_page_renders(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds, _ = _datasette(tmpdir, [db])

    response = await ds.client.get("/-/litestream", cookies=root_cookies(ds))
    assert response.status_code == 200
    assert 'id="app-root"' in response.text
    assert 'id="litestream-page-data"' in response.text
    # The vite entry helper injected the built bundle.
    assert "/-/static-plugins/datasette_litestream/gen/main-" in response.text


# --- status API -------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_status_requires_permission(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds, _ = _datasette(tmpdir, [db])

    response = await ds.client.get("/-/litestream/api/status")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_api_status_payload(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    extra = str(tmpdir / "extra.db")
    table(db, "t").insert({"v": 1})
    table(extra, "t").insert({"v": 1})
    ds, _ = _datasette(tmpdir, [db])
    await ds.invoke_startup()

    # Attach a second db that is NOT registered -> should show as available.
    ds.add_database(Database(ds, path=extra, is_mutable=True), name="extra")

    response = await ds.client.get("/-/litestream/api/status", cookies=root_cookies(ds))
    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is True
    assert payload["can_manage"] is True
    assert payload["daemon"] is not None
    assert payload["daemon"]["version"]

    managed = {d["database"] for d in payload["databases"]}
    assert "data" in managed
    available = {d["database"] for d in payload["available"]}
    assert "extra" in available
    # The available entry carries a suggested replica from replica-url-template.
    extra_row = next(d for d in payload["available"] if d["database"] == "extra")
    assert extra_row["suggested_replica"].endswith("/extra")


@pytest.mark.asyncio
async def test_menu_link_shown_for_db_level_only_config(litestream_binary, tmpdir):
    """Database-level-only configuration runs a daemon, so the menu must link
    the admin page (top-level plugin_config() is None for this shape)."""
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"
    ds = Datasette(
        [db_path],
        config={
            "databases": {
                "data": {
                    "plugins": {
                        "datasette-litestream": {
                            "replica": "file://" + str(backups) + "/data"
                        }
                    }
                }
            }
        },
    )
    ds.root_enabled = True
    await ds.invoke_startup()
    response = await ds.client.get("/", cookies=root_cookies(ds))
    assert "/-/litestream" in response.text


@pytest.mark.asyncio
async def test_menu_link_absent_when_unconfigured():
    ds = Datasette(memory=True)
    ds.root_enabled = True
    await ds.invoke_startup()
    response = await ds.client.get("/", cookies=root_cookies(ds))
    assert "/-/litestream" not in response.text


@pytest.mark.asyncio
async def test_api_status_not_running():
    """No plugin config -> no daemon -> status reports running False."""
    ds = Datasette(memory=True)
    ds.root_enabled = True
    await ds.invoke_startup()
    response = await ds.client.get("/-/litestream/api/status", cookies=root_cookies(ds))
    assert response.status_code == 200
    assert response.json() == {"running": False}


@pytest.mark.asyncio
async def test_api_status_running_false_for_dead_daemon(litestream_binary, tmpdir):
    """A crashed daemon (process object still present) must not report
    running=true — the payload keeps its context fields so the UI can still
    show warnings and available databases."""
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    ds, _backups = _datasette(tmpdir, [db_path])
    await ds.invoke_startup()

    litestream_process = get_process(ds)
    assert litestream_process is not None
    litestream_process.process.kill()
    litestream_process.process.wait()

    response = await ds.client.get("/-/litestream/api/status", cookies=root_cookies(ds))
    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    # Context is still present, unlike the never-started case.
    assert "available" in payload
    assert "warnings" in payload


# --- sync / stop / start ----------------------------------------------------


@pytest.mark.asyncio
async def test_api_sync_stop_start(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds, _ = _datasette(tmpdir, [db])
    await ds.invoke_startup()
    headers = await root_token(ds)

    # sync
    r = await ds.client.post(
        "/-/litestream/api/sync", json={"database": "data"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # stop
    r = await ds.client.post(
        "/-/litestream/api/stop", json={"database": "data"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # start again
    r = await ds.client.post(
        "/-/litestream/api/start", json={"database": "data"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_api_sync_requires_permission(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds, _ = _datasette(tmpdir, [db])
    await ds.invoke_startup()

    # No actor -> forbidden (no manage permission).
    r = await ds.client.post("/-/litestream/api/sync", json={"database": "data"})
    assert r.status_code == 403
