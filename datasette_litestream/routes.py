"""Route handlers for the datasette-litestream admin UI and JSON API.

Handlers register themselves on the shared ``router`` (see ``router.py``);
request/response shapes live in ``contract.py``.
"""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import httpx
from datasette.utils.asgi import Response
from datasette_plugin_router import Body
from prometheus_client.parser import text_string_to_metric_families

from ._client import LitestreamControlError
from .contract import (
    ActionResult,
    DbActionBody,
    RegisterBody,
    Status,
    UnregisterBody,
)
from .process import get_process, redact_credentials
from .replicas import (
    INTERNAL_DB_NAME,
    expand_replica_template,
    internal_database_path,
    resolve_replica_url,
)
from .router import MANAGE_ACTION, VIEW_STATUS_ACTION, permission_required, router


def _resolve_db_path(datasette, db_name):
    """Return the resolved file path for a Datasette database name, or None."""
    db = datasette.databases.get(db_name)
    if db is None and db_name == INTERNAL_DB_NAME:
        internal_path, _ = internal_database_path(datasette)
        return str(internal_path.resolve()) if internal_path else None
    if db is None or db.path is None:
        return None
    return str(Path(db.path).resolve())


def _suggested_replica(datasette, db_name, db_path):
    """Resolve the configured replica URL for a database, if any."""
    plugin_config_db = datasette.plugin_config(
        "datasette-litestream", db_name, fallback=False
    )
    all_replicate = (datasette.plugin_config("datasette-litestream") or {}).get(
        "all-replicate"
    )
    return resolve_replica_url(db_name, Path(db_path), plugin_config_db, all_replicate)


async def _build_status(datasette, litestream_process, can_manage):
    """Assemble the JSON status payload consumed by the admin UI."""
    # Map litestream's absolute paths back to Datasette database names.
    name_by_path = {}
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        name_by_path[str(Path(db.path).resolve())] = db_name
    internal_path, _ = internal_database_path(datasette)
    if internal_path is not None:
        name_by_path.setdefault(str(internal_path.resolve()), INTERNAL_DB_NAME)

    daemon = None
    managed = []
    socket_error = None
    registered_paths = set()
    if litestream_process.client is not None:
        try:
            daemon = await asyncio.to_thread(litestream_process.client.info)
            databases = await asyncio.to_thread(
                litestream_process.client.list_databases
            )
            for entry in databases:
                path = entry.get("path")
                registered_paths.add(path)
                managed.append(
                    {
                        "database": name_by_path.get(path),
                        "path": path,
                        "status": entry.get("status"),
                        "last_sync_at": entry.get("last_sync_at"),
                        "replica": litestream_process.registered.get(path),
                    }
                )
        except Exception as e:
            socket_error = str(e)

    # Attached, file-backed Datasette databases not currently replicating.
    available = []
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        resolved = str(Path(db.path).resolve())
        if resolved in registered_paths:
            continue
        available.append(
            {
                "database": db_name,
                "path": resolved,
                "suggested_replica": _suggested_replica(datasette, db_name, resolved),
            }
        )

    # A persistent internal database can be registered like any other.
    if internal_path is not None:
        resolved = str(internal_path.resolve())
        if resolved not in registered_paths:
            plugin_config_top = datasette.plugin_config("datasette-litestream") or {}
            replicate_internal = plugin_config_top.get("replicate-internal")
            if isinstance(replicate_internal, str):
                suggested = expand_replica_template(
                    replicate_internal, INTERNAL_DB_NAME, internal_path
                )
            else:
                suggested = resolve_replica_url(
                    INTERNAL_DB_NAME,
                    internal_path,
                    None,
                    plugin_config_top.get("all-replicate"),
                )
            available.append(
                {
                    "database": INTERNAL_DB_NAME,
                    "path": resolved,
                    "suggested_replica": suggested,
                }
            )

    return {
        "running": True,
        "can_manage": can_manage,
        "metrics_enabled": litestream_process.metrics_addr is not None,
        "daemon": daemon,
        "socket_error": socket_error,
        "databases": managed,
        "available": available,
        "warnings": litestream_process.warnings,
    }


@router.GET(r"^/-/litestream$")
@permission_required(VIEW_STATUS_ACTION)
async def litestream_admin_page(datasette, request):
    """GET /-/litestream — the Svelte admin UI (read-only without manage)."""
    can_manage = await datasette.allowed(actor=request.actor, action=MANAGE_ACTION)
    return Response.html(
        await datasette.render_template(
            "litestream_admin.html",
            context={
                "page_data": {
                    "can_manage": bool(can_manage),
                    "actor": request.actor,
                },
            },
            request=request,
        )
    )


@router.GET(r"^/-/litestream/api/status$", output=Status)
@permission_required(VIEW_STATUS_ACTION)
async def litestream_api_status(datasette, request):
    """GET /-/litestream/api/status — JSON status snapshot for the UI to poll."""
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json({"running": False})

    can_manage = await datasette.allowed(actor=request.actor, action=MANAGE_ACTION)
    return Response.json(
        await _build_status(datasette, litestream_process, bool(can_manage))
    )


async def _db_action(datasette, db_name, method_name, **kwargs):
    """Shared handler for sync/start/stop: resolve the db and call the client.

    Permission checks happen in the route decorators before this runs.
    """
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    if not db_name:
        return Response.json(
            {"ok": False, "error": "'database' is required"}, status=400
        )

    db_path = _resolve_db_path(datasette, db_name)
    if db_path is None:
        return Response.json(
            {"ok": False, "error": f"unknown or in-memory database: {db_name}"},
            status=404,
        )

    method = getattr(litestream_process.client, method_name)
    try:
        result = await asyncio.to_thread(method, db_path, **kwargs)
    except LitestreamControlError as e:
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details}, status=502
        )
    return Response.json({"ok": True, "database": db_name, "result": result})


@router.POST(r"^/-/litestream/api/sync$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_api_sync(datasette, request, body: Annotated[DbActionBody, Body()]):
    """POST /-/litestream/api/sync  {"database": "<name>"}"""
    return await _db_action(datasette, body.database, "sync", wait=True)


@router.POST(r"^/-/litestream/api/start$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_api_start(
    datasette, request, body: Annotated[DbActionBody, Body()]
):
    """POST /-/litestream/api/start  {"database": "<name>"}"""
    return await _db_action(datasette, body.database, "start")


@router.POST(r"^/-/litestream/api/stop$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_api_stop(datasette, request, body: Annotated[DbActionBody, Body()]):
    """POST /-/litestream/api/stop  {"database": "<name>"}"""
    return await _db_action(datasette, body.database, "stop")


@router.POST(r"^/-/litestream/register$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_register(datasette, request, body: Annotated[RegisterBody, Body()]):
    """POST /-/litestream/register  {"database": "<name>", "replica": "<url?>"}

    Registers a currently-attached Datasette database with the running litestream
    daemon at runtime. The replica URL may be supplied in the body, otherwise it
    is resolved from the plugin's ``all-replicate`` / db-level config.
    """
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    db_name = body.database
    if not db_name:
        return Response.json(
            {"ok": False, "error": "'database' is required"}, status=400
        )

    db_path = _resolve_db_path(datasette, db_name)
    if db_path is None:
        return Response.json(
            {"ok": False, "error": f"unknown or in-memory database: {db_name}"},
            status=404,
        )

    replica_url = body.replica
    if not replica_url:
        plugin_config_db = datasette.plugin_config(
            "datasette-litestream", db_name, fallback=False
        )
        all_replicate = (
            datasette.plugin_config("datasette-litestream") or {}
        ).get("all-replicate")
        replica_url = resolve_replica_url(
            db_name, Path(db_path), plugin_config_db, all_replicate
        )
    if not replica_url:
        return Response.json(
            {
                "ok": False,
                "error": "no replica URL provided or configured for this database",
            },
            status=400,
        )

    try:
        result = await asyncio.to_thread(
            litestream_process.register_db, db_path, replica_url
        )
    except LitestreamControlError as e:
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details},
            status=502,
        )

    return Response.json(
        {
            "ok": True,
            "database": db_name,
            "path": db_path,
            "replica": replica_url,
            "status": result.get("status"),
        }
    )


@router.POST(r"^/-/litestream/unregister$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_unregister(
    datasette, request, body: Annotated[UnregisterBody, Body()]
):
    """POST /-/litestream/unregister  {"database": "<name>", "timeout": <int?>}

    Removes a database from litestream replication at runtime. The daemon
    performs a final sync to the replica before dropping it.
    """
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    db_name = body.database
    if not db_name:
        return Response.json(
            {"ok": False, "error": "'database' is required"}, status=400
        )

    # Prefer the attached database's path; fall back to any path we registered
    # under this name in case the database was already detached from Datasette.
    db_path = _resolve_db_path(datasette, db_name)
    if db_path is None:
        return Response.json(
            {"ok": False, "error": f"unknown or in-memory database: {db_name}"},
            status=404,
        )

    timeout = body.timeout
    try:
        result = await asyncio.to_thread(
            litestream_process.unregister_db, db_path, timeout
        )
    except LitestreamControlError as e:
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details},
            status=502,
        )

    return Response.json(
        {
            "ok": True,
            "database": db_name,
            "path": db_path,
            "status": result.get("status"),
            "txid": result.get("txid"),
        }
    )


@router.GET(r"^/-/litestream-status$")
@permission_required(VIEW_STATUS_ACTION)
async def litestream_status(datasette, request):
    """GET /-/litestream-status — the legacy server-rendered status page."""
    litestream_process = get_process(datasette)

    if litestream_process is None:
        return Response.html("<h1>Litestream not running</h1>")

    # Map litestream's absolute database paths back to Datasette names.
    db_name_lookup = {}
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        db_name_lookup[str(Path(db.path).resolve())] = db_name

    # Live daemon state from the control socket (best effort).
    daemon_info = None
    managed = []
    socket_error = None
    if litestream_process.client is not None:
        try:
            daemon_info = await asyncio.to_thread(litestream_process.client.info)
            databases = await asyncio.to_thread(
                litestream_process.client.list_databases
            )
            for entry in databases:
                managed.append(
                    {
                        "database": db_name_lookup.get(entry.get("path")),
                        "path": entry.get("path"),
                        "status": entry.get("status"),
                        "last_sync_at": entry.get("last_sync_at"),
                    }
                )
        except LitestreamControlError as e:
            socket_error = str(e)
        except Exception as e:
            socket_error = str(e)

    replica_operations = {"bytes": [], "total": []}
    metrics_by_db = {}
    go_stats = {}

    metrics_enabled = litestream_process.metrics_addr is not None

    if metrics_enabled:
        addr = litestream_process.metrics_addr
        # TODO detect when non-localhost addresses are used
        try:
            metrics_page = httpx.get(f"http://localhost{addr}/metrics").text
        except Exception:
            metrics_page = ""

        for family in text_string_to_metric_families(metrics_page):
            for sample in family.samples:
                # litestream 0.5 renamed the bytes counter (dropped the _total suffix).
                if sample.name in (
                    "litestream_replica_operation_bytes",
                    "litestream_replica_operation_bytes_total",
                ):
                    replica_operations["bytes"].append(
                        {**sample.labels, "value": sample.value}
                    )
                elif sample.name == "litestream_replica_operation_total":
                    replica_operations["total"].append(
                        {**sample.labels, "value": sample.value}
                    )
                elif sample.name.startswith("litestream_"):
                    db_path = sample.labels.get("db")
                    if db_path is None:
                        continue
                    db = db_name_lookup.get(db_path)
                    if db is None:
                        # Path from metrics may not match resolved path
                        continue
                    metrics_by_db.setdefault(db, {})[sample.name] = sample.value
                elif sample.name in ["go_goroutines", "go_threads"]:
                    go_stats[sample.name] = sample.value

    return Response.html(
        await datasette.render_template(
            "litestream.html",
            context={
                "process": {
                    "pid": litestream_process.process.pid,
                    "status": (
                        "alive" if litestream_process.process.poll() is None else "died"
                    ),
                    "socket": litestream_process.socket_path,
                },
                "daemon_info": daemon_info,
                "managed_databases": managed,
                "socket_error": socket_error,
                "logs": open(litestream_process.logfile.name, "r").read(),
                "metrics_enabled": metrics_enabled,
                "litestream_config": json.dumps(
                    redact_credentials(litestream_process.daemon_config or {}), indent=2
                ),
                "replica_operations": replica_operations,
                "metrics_by_db": metrics_by_db,
                "go_stats": go_stats,
            },
            request=request,
        )
    )
