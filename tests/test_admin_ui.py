"""Tests for the Svelte/Vite admin UI: the page route, the JSON status API,
and the sync/start/stop endpoints (all gated by litestream permissions)."""

import pytest
from conftest import table
from datasette.app import Datasette
from datasette.database import Database

actor_root = {"a": {"id": "root"}}


def _datasette(tmpdir, db_paths):
    backups = tmpdir / "backups"
    ds = Datasette(
        [str(p) for p in db_paths],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"]
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
    # The available entry carries a suggested replica from all-replicate.
    extra_row = next(d for d in payload["available"] if d["database"] == "extra")
    assert extra_row["suggested_replica"].endswith("/extra")


@pytest.mark.asyncio
async def test_api_status_not_running():
    """No plugin config -> no daemon -> status reports running False."""
    ds = Datasette(memory=True)
    ds.root_enabled = True
    await ds.invoke_startup()
    response = await ds.client.get("/-/litestream/api/status", cookies=root_cookies(ds))
    assert response.status_code == 200
    assert response.json() == {"running": False}


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
