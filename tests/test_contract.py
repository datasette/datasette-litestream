"""Tests for the API contract: route registration, the OpenAPI document the
frontend types are generated from, request-body validation, and that live
endpoint payloads actually validate against the contract models."""

import pytest
from conftest import table
from datasette.app import Datasette

import datasette_litestream.routes  # noqa: F401  (registers the handlers)
from datasette_litestream.contract import ActionResult, Status
from datasette_litestream.router import router

actor_root = {"a": {"id": "root"}}

EXPECTED_ROUTES = {
    r"^/-/litestream$",
    r"^/-/litestream/api/status$",
    r"^/-/litestream/api/sync$",
    r"^/-/litestream/api/start$",
    r"^/-/litestream/api/stop$",
    r"^/-/litestream/register$",
    r"^/-/litestream/unregister$",
}


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
    return ds


async def root_token(datasette):
    token = await datasette.create_token("root")
    return {"Authorization": f"Bearer {token}"}


# --- Route registration and the OpenAPI document ---------------------------


def test_router_registers_all_routes():
    assert {path for path, _ in router.routes()} == EXPECTED_ROUTES


def test_openapi_document_covers_all_routes():
    """The document behind `just types-routes` lists every route, and the JSON
    endpoints carry response schemas for the type generation."""
    doc = router.openapi_document_json()
    assert set(doc["paths"]) == {
        "/-/litestream",
        "/-/litestream/api/status",
        "/-/litestream/api/sync",
        "/-/litestream/api/start",
        "/-/litestream/api/stop",
        "/-/litestream/register",
        "/-/litestream/unregister",
    }
    status_response = doc["paths"]["/-/litestream/api/status"]["get"]["responses"][
        "200"
    ]
    assert "application/json" in status_response["content"]
    sync_op = doc["paths"]["/-/litestream/api/sync"]["post"]
    assert "requestBody" in sync_op
    assert "application/json" in sync_op["responses"]["200"]["content"]
    # Nested models referenced by Status land in components.schemas.
    assert {"DaemonInfo", "ManagedDatabase", "AvailableDatabase"} <= set(
        doc.get("components", {}).get("schemas", {})
    )


# --- Request body validation ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"",  # empty body
        b"not json",  # malformed JSON
        b'{"database": 123}',  # wrong type
        b"{}",  # missing required field
    ],
)
async def test_invalid_body_returns_400(body):
    """Pydantic validation rejects bad bodies with a structured 400 (the router
    binds the body before the permission check runs, so no actor is needed)."""
    ds = Datasette(memory=True)
    await ds.invoke_startup()
    response = await ds.client.post(
        "/-/litestream/api/sync",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    payload = response.json()
    assert "error" in payload
    assert isinstance(payload["errors"], list)


@pytest.mark.asyncio
async def test_negative_unregister_timeout_returns_400():
    ds = Datasette(memory=True)
    await ds.invoke_startup()
    response = await ds.client.post(
        "/-/litestream/unregister",
        json={"database": "data", "timeout": -5},
    )
    assert response.status_code == 400
    assert "error" in response.json()


# --- Live payloads validate against the contract models ---------------------


@pytest.mark.asyncio
async def test_status_payload_matches_contract(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds = _datasette(tmpdir, [db])
    await ds.invoke_startup()

    response = await ds.client.get(
        "/-/litestream/api/status",
        cookies={"ds_actor": ds.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    status = Status.model_validate(response.json())
    assert status.running is True
    assert status.daemon is not None
    assert status.databases is not None
    assert any(d.database == "data" for d in status.databases)


@pytest.mark.asyncio
async def test_status_not_running_matches_contract():
    ds = Datasette(memory=True)
    ds.root_enabled = True
    await ds.invoke_startup()
    response = await ds.client.get(
        "/-/litestream/api/status",
        cookies={"ds_actor": ds.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    status = Status.model_validate(response.json())
    assert status.running is False


@pytest.mark.asyncio
async def test_action_payloads_match_contract(litestream_binary, tmpdir):
    db = str(tmpdir / "data.db")
    table(db, "t").insert({"v": 1})
    ds = _datasette(tmpdir, [db])
    await ds.invoke_startup()
    headers = await root_token(ds)

    sync = await ds.client.post(
        "/-/litestream/api/sync", json={"database": "data"}, headers=headers
    )
    assert sync.status_code == 200
    assert ActionResult.model_validate(sync.json()).ok is True

    unregister = await ds.client.post(
        "/-/litestream/unregister", json={"database": "data"}, headers=headers
    )
    assert unregister.status_code == 200
    result = ActionResult.model_validate(unregister.json())
    assert result.ok is True
    assert result.database == "data"

    # Error payloads are ActionResults too.
    missing = await ds.client.post(
        "/-/litestream/api/sync", json={"database": "nope"}, headers=headers
    )
    assert missing.status_code == 404
    assert ActionResult.model_validate(missing.json()).ok is False
