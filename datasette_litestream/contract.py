"""Pydantic models describing the datasette-litestream JSON API contract.

These are the single source of truth for the API's wire format: the route
handlers validate request bodies against the request models, and the response
models feed the OpenAPI document (``router.openapi_document_json()``) that the
frontend TypeScript types (frontend/api.d.ts) are generated from.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Replica URL schemes supported by litestream 0.5 (its registered replica
# client factories); anything else is rejected at the API edge.
ALLOWED_REPLICA_SCHEMES = {
    "abs",
    "file",
    "gs",
    "nats",
    "oss",
    "s3",
    "sftp",
    "webdav",
    "webdavs",
}

# --- Request bodies ---------------------------------------------------------


class TargetBody(BaseModel):
    """Base for bodies addressing a database.

    An attached Datasette database is addressed by name; the internal database
    is addressed with ``internal: true`` (never by name — a legitimately
    attached database could be called anything, including ``_internal``).
    """

    database: str | None = None
    internal: bool = False

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if self.internal and self.database:
            raise ValueError("pass either 'database' or 'internal': true, not both")
        if not self.internal and not self.database:
            raise ValueError("either 'database' or 'internal': true is required")
        return self


class DbActionBody(TargetBody):
    """Body for sync/start/stop."""


class RegisterBody(TargetBody):
    """Body for /-/litestream/register."""

    # Optional replica URL; falls back to the configured replica for the target.
    replica: str | None = None

    @field_validator("replica")
    @classmethod
    def _replica_scheme_allowed(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            # Same meaning as omitting the field: use the configured replica.
            return None
        scheme, sep, _ = value.partition("://")
        if not sep or not scheme:
            raise ValueError(
                "replica URL must include an explicit scheme, e.g. 's3://' or 'file://'"
            )
        if scheme.lower() not in ALLOWED_REPLICA_SCHEMES:
            raise ValueError(
                f"unsupported replica URL scheme '{scheme}'; allowed schemes: "
                + ", ".join(sorted(ALLOWED_REPLICA_SCHEMES))
            )
        return value


class UnregisterBody(TargetBody):
    """Body for /-/litestream/unregister."""

    # Seconds to wait for the daemon's final sync before giving up. The daemon
    # takes whole seconds; fractional values are rounded up on the wire.
    # gt=0: the daemon's protocol treats 0 as "use the default", the opposite
    # of what a caller asking for zero wait means. allow_inf_nan: "inf" would
    # otherwise pass ge and overflow math.ceil in the client.
    timeout: float | None = Field(default=None, gt=0, allow_inf_nan=False)


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
    # None for the internal database, which is identified by ``internal``.
    database: str | None = None
    internal: bool = False
    path: str
    status: str | None = None
    last_sync_at: str | None = None
    replica: str | None = None


class AvailableDatabase(BaseModel):
    """A file-backed database not currently replicating."""

    # None for the internal database, which is identified by ``internal``.
    database: str | None = None
    internal: bool = False
    path: str
    suggested_replica: str | None = None


class Status(BaseModel):
    """Payload of GET /-/litestream/api/status.

    Only ``running`` is always present. When the plugin never started a
    daemon the endpoint returns ``{"running": false}`` and nothing else;
    when the daemon was started but has died (crash, failed rotation
    restart), ``running`` is false and the rest of the payload — warnings,
    available databases — is still populated so the UI can show context.
    """

    running: bool
    can_manage: bool | None = None
    metrics_enabled: bool | None = None
    daemon: DaemonInfo | None = None
    socket_error: str | None = None
    databases: list[ManagedDatabase] | None = None
    available: list[AvailableDatabase] | None = None
    warnings: list[str] | None = None


class ActionResult(BaseModel):
    """Payload of the POST endpoints (sync/start/stop/register/unregister)."""

    ok: bool
    error: str | None = None
    details: str | None = None
    database: str | None = None
    internal: bool | None = None
    path: str | None = None
    replica: str | None = None
    status: str | None = None
    txid: Any | None = None
    result: Any | None = None
