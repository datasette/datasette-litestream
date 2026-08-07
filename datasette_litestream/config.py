"""Typed plugin configuration for datasette-litestream.

Pydantic models for the top-level and per-database ``datasette-litestream``
config blocks. Config is validated once at startup (``extra="forbid"``, so a
typo'd key fails with a clear error instead of being silently ignored) and the
parsed ``LitestreamConfig`` is cached on the Datasette instance for route
handlers via ``get_config()``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLUGIN_NAME = "datasette-litestream"

# Parsed LitestreamConfig is cached on the Datasette object under this attr.
_CONFIG_ATTR = "__DATASETTE_LITESTREAM_CONFIG__"


def _kebab(name: str) -> str:
    return name.replace("_", "-")


class Credentials(BaseModel):
    """S3 credentials, passed to the litestream daemon via AWS_* env vars.

    Every field is optional because the static config keys may set any subset.
    Extra keys are ignored rather than forbidden: credential commands often
    emit additional metadata (e.g. an STS expiration timestamp).
    """

    model_config = ConfigDict(
        alias_generator=_kebab, populate_by_name=True, extra="ignore"
    )

    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None


class LoadedCredentials(Credentials):
    """Credentials loaded from a credentials ``file`` / ``command``, where the
    access-key-id / secret-access-key pair is required."""

    access_key_id: str
    secret_access_key: str


class CredentialsConfig(Credentials):
    """``credentials`` config block: static keys, or a dynamic source.

    Extends ``Credentials`` with the dynamic-credential options
    (mutually exclusive ``file``/``command``, plus their required
    ``refresh-interval``). Unlike ``Credentials`` this is operator-written
    config, so unknown keys are forbidden like the other config models.
    """

    model_config = ConfigDict(
        alias_generator=_kebab, populate_by_name=True, extra="forbid"
    )

    # Dynamic credentials: a JSON file to read, or a command emitting JSON.
    file: str | None = None
    command: str | None = None
    # How often (seconds) to re-check the file/command for rotated credentials.
    # Floor of 1s: the check runs a subprocess or file read, and asyncio.sleep
    # on a zero/negative interval would degenerate into a busy loop.
    refresh_interval: float | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_options(self):
        if self.file and self.command:
            raise ValueError("cannot specify both a credentials 'file' and 'command'")
        if self.uses_dynamic:
            if not self.refresh_interval:
                raise ValueError(
                    "credentials 'refresh-interval' is required when using a "
                    "credentials 'file' or 'command'"
                )
            if self.access_key_id or self.secret_access_key or self.session_token:
                raise ValueError(
                    "static credentials ('access-key-id', 'secret-access-key', "
                    "'session-token') cannot be combined with a credentials "
                    "'file' or 'command' — the dynamic source provides them"
                )
        elif self.refresh_interval is not None:
            raise ValueError(
                "credentials 'refresh-interval' has no effect without a "
                "credentials 'file' or 'command'"
            )
        return self

    @property
    def uses_dynamic(self) -> bool:
        return bool(self.file or self.command)

    @property
    def static(self) -> Credentials | None:
        """Credentials from the static keys, or None if none are set."""
        if not (self.access_key_id or self.secret_access_key or self.session_token):
            return None
        return Credentials(
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            session_token=self.session_token,
        )


class LoggingConfig(BaseModel):
    """``logging`` config block, passed through to the litestream daemon.

    The daemon always runs with litestream's ``logging.stderr: true`` so its
    output lands in the plugin's logfile (shown on the admin page and dumped
    on startup failure) instead of the console; ``path`` redirects that
    logfile somewhere durable.
    """

    model_config = ConfigDict(
        alias_generator=_kebab, populate_by_name=True, extra="forbid"
    )

    level: Literal["debug", "info", "warn", "error"] = "info"
    type: Literal["text", "json"] = "text"
    # Where litestream's log output is written (opened in append mode).
    # None = a session-scoped temp file. Created with 0600 permissions; a
    # pre-existing file keeps whatever permissions the operator gave it.
    path: str | None = None


class DatabaseConfig(BaseModel):
    """Per-database config block (``databases.<name>.plugins.datasette-litestream``)."""

    model_config = ConfigDict(
        alias_generator=_kebab, populate_by_name=True, extra="forbid"
    )

    replica: str | None = None
    # The litestream <= 0.3 multi-replica list. litestream 0.5 replicates to a
    # single destination, so this is rejected with a migration hint (rather
    # than left to extra="forbid") to make clear entries would be dropped.
    replicas: list[str | dict] | None = None

    @model_validator(mode="after")
    def _reject_deprecated_replicas(self):
        if self.replicas is not None:
            raise ValueError(
                "'replicas' is no longer supported — litestream 0.5 replicates "
                "each database to a single destination; use 'replica' with one "
                "URL instead"
            )
        return self


class LitestreamConfig(BaseModel):
    """Top-level config block (``plugins.datasette-litestream``)."""

    model_config = ConfigDict(
        alias_generator=_kebab, populate_by_name=True, extra="forbid"
    )

    # Template replica URL applied to every attached database.
    replica_url_template: str | None = None
    # Replica URL for Datasette's internal database (requires --internal).
    # Used literally — no template variables, since it names one specific
    # database rather than a pattern applied across many.
    internal_replica_url: str | None = None
    # litestream metrics/pprof bind address (e.g. ":9090").
    metrics_addr: str | None = None
    # When true, the runtime register API only accepts the replica URL derived
    # from configuration (db-level 'replica' or the 'replica-url-template'
    # pattern); caller-supplied URLs that differ are rejected with a 400.
    restrict_runtime_replicas: bool = False
    # litestream daemon logging: level, format and destination file.
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    # S3 credentials: static keys, or a dynamic file/command source.
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)

    @field_validator("replica_url_template", mode="before")
    @classmethod
    def _reject_list(cls, value):
        # Pre-0.5 versions accepted a list here and used the first entry;
        # reject it with a migration hint instead of a bare type error.
        if isinstance(value, (list, tuple)):
            # Not a TypeError: pydantic only turns ValueError into a
            # ValidationError; a TypeError would escape validation.
            raise ValueError(  # noqa: TRY004
                "'replica-url-template' must be a single URL template, not a "
                "list — litestream 0.5 replicates each database to a single "
                "destination"
            )
        return value


def get_config(datasette) -> LitestreamConfig:
    """Return the validated top-level config, cached on the Datasette instance.

    Startup parses (and caches) this first, so a ``ValidationError`` normally
    surfaces there as a ``StartupError``.
    """
    cached = getattr(datasette, _CONFIG_ATTR, None)
    if cached is not None:
        return cached
    config = LitestreamConfig.model_validate(datasette.plugin_config(PLUGIN_NAME) or {})
    setattr(datasette, _CONFIG_ATTR, config)
    return config


def get_database_config(datasette, db_name: str) -> DatabaseConfig | None:
    """Return the validated db-level config, or None when none is configured.

    The None-vs-present distinction matters: the presence of a db-level block
    (even an empty one) opts that database into replication at startup.
    """
    raw = datasette.plugin_config(PLUGIN_NAME, db_name, fallback=False)
    if raw is None:
        return None
    return DatabaseConfig.model_validate(raw)
