"""Pydantic models describing the datasette-litestream JSON API contract.

These are the single source of truth for the API's wire format: the route
handlers validate request bodies against the request models, and the response
models feed the OpenAPI document (``router.openapi_document_json()``) that the
frontend TypeScript types (frontend/api.d.ts) are generated from.
"""

from typing import Any, Optional

from pydantic import BaseModel


# --- Request bodies ---------------------------------------------------------


class DbActionBody(BaseModel):
    """Body for sync/start/stop: name a Datasette database."""

    database: str


class RegisterBody(BaseModel):
    """Body for /-/litestream/register."""

    database: str
    # Optional replica URL; falls back to db-level / all-replicate config.
    replica: Optional[str] = None


class UnregisterBody(BaseModel):
    """Body for /-/litestream/unregister."""

    database: str
    # Seconds to wait for the daemon's final sync before giving up.
    timeout: Optional[float] = None


# --- Response payloads ------------------------------------------------------


class DaemonInfo(BaseModel):
    """Daemon-level info reported by litestream's control socket."""

    version: str
    pid: int
    uptime_seconds: float
    started_at: str
    database_count: int


class ManagedDatabase(BaseModel):
    """A database currently registered with the litestream daemon."""

    # Datasette database name, if the path maps back to an attached database.
    database: Optional[str] = None
    path: str
    status: Optional[str] = None
    last_sync_at: Optional[str] = None
    replica: Optional[str] = None


class AvailableDatabase(BaseModel):
    """An attached, file-backed database not currently replicating."""

    database: str
    path: str
    suggested_replica: Optional[str] = None


class Status(BaseModel):
    """Payload of GET /-/litestream/api/status.

    Only ``running`` is always present: when the daemon is not running the
    endpoint returns ``{"running": false}`` and nothing else.
    """

    running: bool
    can_manage: Optional[bool] = None
    metrics_enabled: Optional[bool] = None
    daemon: Optional[DaemonInfo] = None
    socket_error: Optional[str] = None
    databases: Optional[list[ManagedDatabase]] = None
    available: Optional[list[AvailableDatabase]] = None
    warnings: Optional[list[str]] = None


class ActionResult(BaseModel):
    """Payload of the POST endpoints (sync/start/stop/register/unregister)."""

    ok: bool
    error: Optional[str] = None
    details: Optional[str] = None
    database: Optional[str] = None
    path: Optional[str] = None
    replica: Optional[str] = None
    status: Optional[str] = None
    txid: Optional[Any] = None
    result: Optional[Any] = None
