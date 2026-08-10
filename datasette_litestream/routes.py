"""Route handlers for the datasette-litestream admin UI and JSON API.

Handlers register themselves on the shared ``router`` (see ``router.py``);
request/response shapes live in ``contract.py``.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
from datasette.utils.asgi import Response
from datasette_plugin_router import Body

from ._client import LitestreamControlError
from .config import get_config, get_database_config
from .contract import (
    ActionResult,
    DbActionBody,
    RegisterBody,
    Status,
    UnregisterBody,
)
from .process import get_process
from .replicas import internal_database_path, resolve_replica_url
from .router import MANAGE_ACTION, VIEW_STATUS_ACTION, permission_required, router


def _resolve_db_path(datasette, db_name):
    """Return the resolved file path for an attached database name, or None."""
    db = datasette.databases.get(db_name)
    if db is None or db.path is None:
        return None
    return str(Path(db.path).resolve())


def _resolve_target(datasette, litestream_process, body, fallback_registered=True):
    """Resolve a request body's target to (db_path, db_name, error_response).

    ``db_name`` is None for the internal database, which is addressed by the
    body's ``internal`` flag and identified by path — never by name, since an
    attached database could legitimately be called anything. For named
    databases, ``fallback_registered`` also consults the paths recorded at
    registration time, so a database detached from Datasette can still be
    synced or unregistered.
    """
    if body.internal:
        internal_path, reason = internal_database_path(datasette)
        if internal_path is None:
            return (
                None,
                None,
                Response.json(
                    {"ok": False, "error": f"no usable internal database: {reason}"},
                    status=400,
                ),
            )
        return str(internal_path.resolve()), None, None
    db_path = _resolve_db_path(datasette, body.database)
    if db_path is None and fallback_registered:
        db_path = litestream_process.registered_names.get(body.database)
    if db_path is None:
        return (
            None,
            None,
            Response.json(
                {
                    "ok": False,
                    "error": f"unknown or in-memory database: {body.database}",
                },
                status=404,
            ),
        )
    return db_path, body.database, None


def _suggested_replica(datasette, db_name, db_path):
    """Resolve the configured replica URL for an attached database, if any."""
    return resolve_replica_url(
        db_name,
        Path(db_path),
        get_database_config(datasette, db_name),
        get_config(datasette).replica_url_template,
    )


# Exceptions a control-socket call can raise short of a programming error:
# LitestreamControlError (daemon replied with an error), httpx transport
# failures (daemon crashed / socket gone / timed out), and RuntimeError from
# _require_client (daemon mid-restart during credential rotation).
DAEMON_ERRORS = (LitestreamControlError, httpx.HTTPError, RuntimeError)


def _daemon_error_response(e):
    """Map a control-socket failure to a JSON 5xx ActionResult response."""
    if isinstance(e, LitestreamControlError):
        return Response.json(
            {"ok": False, "error": str(e), "details": e.details}, status=502
        )
    if isinstance(e, httpx.TimeoutException):
        return Response.json(
            {"ok": False, "error": "timed out talking to the litestream daemon"},
            status=504,
        )
    return Response.json(
        {"ok": False, "error": "the litestream daemon is restarting or unavailable"},
        status=503,
    )


async def _build_status(datasette, litestream_process, can_manage):
    """Assemble the JSON status payload consumed by the admin UI."""
    # Map litestream's absolute paths back to Datasette database names. The
    # internal database is matched by path and flagged, never named.
    name_by_path = {}
    for db_name, db in datasette.databases.items():
        if db.path is None:
            continue
        name_by_path[str(Path(db.path).resolve())] = db_name
    internal_path, _ = internal_database_path(datasette)
    internal_resolved = (
        str(internal_path.resolve()) if internal_path is not None else None
    )
    # Databases registered with the daemon but since detached from Datasette
    # keep their last-known name.
    for db_name, path in litestream_process.registered_names.items():
        name_by_path.setdefault(path, db_name)

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
                internal = path == internal_resolved
                managed.append(
                    {
                        "database": None if internal else name_by_path.get(path),
                        "internal": internal,
                        "path": path,
                        "status": entry.get("status"),
                        "last_sync_at": entry.get("last_sync_at"),
                        "replica": litestream_process.registered.get(path),
                    }
                )
        except Exception as e:  # noqa: BLE001 -- best effort, surfaced in the UI
            socket_error = str(e)

    # Attached, file-backed, mutable Datasette databases not currently
    # replicating. Immutable ones are excluded: litestream would rewrite
    # them (WAL journal mode), breaking Datasette's immutability promise.
    available = []
    for db_name, db in datasette.databases.items():
        if db.path is None or not db.is_mutable:
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
    if internal_resolved is not None and internal_resolved not in registered_paths:
        available.append(
            {
                "database": None,
                "internal": True,
                "path": internal_resolved,
                "suggested_replica": get_config(datasette).internal_replica_url,
            }
        )

    warnings = list(litestream_process.warnings)
    if litestream_process.health_warning:
        warnings.append(litestream_process.health_warning)

    return {
        # Honest liveness (poll() based): a crashed daemon or a failed
        # rotation restart must not report running=true with a null payload.
        "running": litestream_process.daemon_alive,
        "can_manage": can_manage,
        "metrics_enabled": litestream_process.metrics_addr is not None,
        "daemon": daemon,
        "socket_error": socket_error,
        "databases": managed,
        "available": available,
        "warnings": warnings,
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


async def _db_action(datasette, body, method_name, **kwargs):
    """Shared handler for sync/start/stop: resolve the target and call the client.

    Permission checks happen in the route decorators before this runs.
    """
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    db_path, db_name, error = _resolve_target(datasette, litestream_process, body)
    if error is not None:
        return error

    try:
        method = getattr(litestream_process._require_client(), method_name)
        result = await asyncio.to_thread(method, db_path, **kwargs)
    except DAEMON_ERRORS as e:
        return _daemon_error_response(e)
    return Response.json(
        {"ok": True, "database": db_name, "internal": body.internal, "result": result}
    )


@router.POST(r"^/-/litestream/api/sync$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_api_sync(
    datasette, request, body: Annotated[DbActionBody, Body()]
):
    """POST /-/litestream/api/sync  {"database": "<name>"}"""
    # The control-socket client's 30s transport timeout bounds this blocking
    # sync; an over-long first sync surfaces as a 504 rather than hanging.
    return await _db_action(datasette, body, "sync", wait=True)


@router.POST(r"^/-/litestream/api/start$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_api_start(
    datasette, request, body: Annotated[DbActionBody, Body()]
):
    """POST /-/litestream/api/start  {"database": "<name>"}"""
    return await _db_action(datasette, body, "start")


@router.POST(r"^/-/litestream/api/stop$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_api_stop(
    datasette, request, body: Annotated[DbActionBody, Body()]
):
    """POST /-/litestream/api/stop  {"database": "<name>"}"""
    return await _db_action(datasette, body, "stop")


@router.POST(r"^/-/litestream/register$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_register(
    datasette, request, body: Annotated[RegisterBody, Body()]
):
    """POST /-/litestream/register  {"database": "<name>"|"internal": true, "replica": "<url?>"}

    Registers a currently-attached Datasette database (or the internal
    database) with the running litestream daemon at runtime. The replica URL
    may be supplied in the body, otherwise it is resolved from the plugin's
    configuration (db-level ``replica`` / ``replica-url-template``, or
    ``internal-replica-url`` for the internal database).
    """
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    db_path, db_name, error = _resolve_target(
        datasette, litestream_process, body, fallback_registered=False
    )
    if error is not None:
        return error

    # Never hand litestream an immutable database: it would flip the file to
    # WAL journal mode. (The internal database is always mutable.)
    db = datasette.databases.get(db_name) if db_name is not None else None
    if db is not None and not db.is_mutable:
        return Response.json(
            {"ok": False, "error": f"database '{db_name}' is immutable"},
            status=400,
        )

    replica_url = body.replica
    if body.internal:
        suggested = get_config(datasette).internal_replica_url
    else:
        suggested = _suggested_replica(datasette, db_name, db_path)
    if not replica_url:
        replica_url = suggested
    elif get_config(datasette).restrict_runtime_replicas and replica_url != suggested:
        return Response.json(
            {
                "ok": False,
                "error": "'restrict-runtime-replicas' is enabled: replica URLs "
                "are limited to the configured destination for this database",
            },
            status=400,
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
            litestream_process.register_db, db_path, replica_url, db_name
        )
    except DAEMON_ERRORS as e:
        return _daemon_error_response(e)

    return Response.json(
        {
            "ok": True,
            "database": db_name,
            "internal": body.internal,
            "path": db_path,
            # From the post-registration map, not the request: on
            # already_registered the daemon kept its earlier replica URL,
            # and the response must not claim one it isn't using.
            "replica": litestream_process.registered.get(db_path),
            "status": result.get("status"),
        }
    )


@router.POST(r"^/-/litestream/unregister$", output=ActionResult)
@permission_required(MANAGE_ACTION)
async def litestream_unregister(
    datasette, request, body: Annotated[UnregisterBody, Body()]
):
    """POST /-/litestream/unregister  {"database": "<name>"|"internal": true, "timeout": <int?>}

    Removes a database from litestream replication at runtime. The daemon
    performs a final sync to the replica before dropping it.
    """
    litestream_process = get_process(datasette)
    if litestream_process is None:
        return Response.json(
            {"ok": False, "error": "litestream is not running"}, status=503
        )

    db_path, db_name, error = _resolve_target(datasette, litestream_process, body)
    if error is not None:
        return error

    timeout = body.timeout
    try:
        result = await asyncio.to_thread(
            litestream_process.unregister_db, db_path, timeout
        )
    except DAEMON_ERRORS as e:
        return _daemon_error_response(e)

    return Response.json(
        {
            "ok": True,
            "database": db_name,
            "internal": body.internal,
            "path": db_path,
            "status": result.get("status"),
            "txid": result.get("txid"),
        }
    )
