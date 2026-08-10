"""Datasette plugin hooks for datasette-litestream.

Package layout (mirrors the other datasette-* plugins):

- ``router.py``   — shared route registry + ``permission_required`` decorator
- ``routes.py``   — route handlers for the admin UI and JSON API
- ``contract.py`` — Pydantic request/response models (the API contract)
- ``process.py``  — the litestream daemon: lifecycle, credentials, registry
- ``replicas.py`` — replica URL / internal database resolution helpers
"""

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
from datasette import hookimpl
from datasette.permissions import Action
from datasette.utils import StartupError
from datasette_vite import vite_entry
from pydantic import ValidationError

from . import routes  # noqa: F401  (imports register the route handlers)
from ._client import LitestreamControlError
from .config import LitestreamConfig, get_config, get_database_config
from .contract import validate_replica_url
from .process import (
    DATASETTE_LITESTREAM_PROCESS_KEY,
    LitestreamProcess,
    credentials_hash,
    get_dynamic_credentials,
    get_process,
    processes,
)
from .replicas import internal_database_path, resolve_replica_url
from .router import MANAGE_ACTION, VIEW_STATUS_ACTION, router


@hookimpl
def register_actions(datasette):
    return [
        Action(
            name=VIEW_STATUS_ACTION,
            description="View litestream statistics and status updates.",
        ),
        Action(
            name=MANAGE_ACTION,
            description="Add or remove databases from litestream replication at runtime.",
        ),
    ]


@hookimpl
def register_routes():
    return router.routes()


@hookimpl
def menu_links(datasette, actor):
    async def inner():
        if (
            await datasette.allowed(actor=actor, action=VIEW_STATUS_ACTION)
            # Only link the page when this instance actually started a
            # daemon. Top-level plugin_config() misses instances configured
            # solely at the database level (databases.<name>.plugins...),
            # which run a daemon but got no menu entry.
            and get_process(datasette) is not None
        ):
            return [
                {
                    "href": datasette.urls.path("/-/litestream"),
                    "label": "Litestream",
                },
            ]

    return inner


@hookimpl
def extra_template_vars(datasette):
    entry = vite_entry(
        datasette=datasette,
        plugin_package="datasette_litestream",
    )
    return {"datasette_litestream_vite_entry": entry}


# Health-loop tick for instances without dynamic credentials (which use
# their refresh-interval as the tick instead).
HEALTH_INTERVAL_SECONDS = 30.0


async def health_loop(
    startup_id: str, config: LitestreamConfig, interval_seconds: float
):
    """Background supervisor for the litestream daemon.

    Each tick, in order: with dynamic credentials configured, fetch them and
    restart the daemon when they changed; restart a daemon that is not
    running (crashed, or left down by an earlier failed restart); otherwise
    reconcile the daemon's database list against ``registered``,
    re-registering anything missing (heals a partial re-registration after a
    rotation restart).

    Failures here are never fatal: the daemon keeps replicating with the
    last-known-good credentials (valid until the provider expires them) and
    the loop retries on the next tick. Taking down the whole Datasette
    instance over a replication hiccup would be strictly worse. Repeated
    failures are surfaced as a warning on the admin page so a crash-looping
    binary or a broken credentials source is visible.
    """
    consecutive_failures = 0
    while True:
        await asyncio.sleep(interval_seconds)
        litestream_process = processes.get(startup_id)
        if litestream_process is None:
            return  # Process no longer exists
        try:
            new_creds = None
            if config.credentials.uses_dynamic:
                # In a thread: the file read / credentials command are
                # blocking (the command alone may take up to its 30s
                # subprocess timeout), and this loop shares the event loop
                # with every request handler.
                new_creds = await asyncio.to_thread(get_dynamic_credentials, config)

            if (
                new_creds is not None
                and credentials_hash(new_creds)
                != litestream_process.current_credentials_hash
            ):
                print(
                    "datasette-litestream: credentials changed, restarting litestream",
                    file=sys.stderr,
                )
                await asyncio.to_thread(
                    litestream_process.restart_with_new_credentials, new_creds
                )
            elif not litestream_process.daemon_alive:
                print(
                    "datasette-litestream: daemon is not running, restarting litestream",
                    file=sys.stderr,
                )
                await asyncio.to_thread(
                    litestream_process.restart_with_new_credentials,
                    new_creds
                    if new_creds is not None
                    else litestream_process.credentials,
                )
            else:
                healed = await asyncio.to_thread(
                    litestream_process.reconcile_registrations
                )
                for path in healed:
                    print(
                        f"datasette-litestream: re-registered {path} with the "
                        "daemon (was missing from its database list)",
                        file=sys.stderr,
                    )
            consecutive_failures = 0
            litestream_process.health_warning = None

        except Exception as e:  # noqa: BLE001 -- never let a health failure kill the server
            consecutive_failures += 1
            if consecutive_failures >= 3:
                litestream_process.health_warning = (
                    f"litestream health checks are failing "
                    f"({consecutive_failures} consecutive): {e}"
                )
            print(
                f"datasette-litestream: health check failed "
                f"(consecutive failures: {consecutive_failures}), "
                f"will retry: {e}",
                file=sys.stderr,
            )


@hookimpl
def startup(datasette):
    plugin_config_top = datasette.plugin_config("datasette-litestream") or {}

    # Parse and cache the typed config; a typo'd or invalid key fails startup.
    try:
        config = get_config(datasette)
    except ValidationError as e:
        raise StartupError(f"datasette-litestream: invalid configuration: {e}") from e

    # Load credentials from file/command or from static config
    if config.credentials.uses_dynamic:
        try:
            creds = get_dynamic_credentials(config)
        except Exception as e:
            raise StartupError(
                f"datasette-litestream: failed to load initial credentials: {e}"
            ) from e
    else:
        creds = config.credentials.static

    replica_url_template = config.replica_url_template
    warnings = []

    # litestream's metrics server is unauthenticated and also mounts Go's
    # /debug/pprof handlers; warn when it would listen on all interfaces.
    if config.metrics_addr:
        metrics_host = config.metrics_addr.rpartition(":")[0]
        if metrics_host in ("", "0.0.0.0", "[::]", "::"):
            warnings.append(
                f"'metrics-addr' {config.metrics_addr!r} listens on all "
                "interfaces with no authentication (Prometheus metrics and Go "
                "pprof endpoints) — bind it to loopback, e.g. "
                "'127.0.0.1:9090', unless it is firewalled."
            )

    # Work out which databases to replicate at startup.
    initial = []  # list of (db_name, db_path_str, replica_url)
    for db_name, db in datasette.databases.items():
        try:
            db_config = get_database_config(datasette, db_name)
        except ValidationError as e:
            raise StartupError(
                f"datasette-litestream: invalid configuration for database "
                f"'{db_name}': {e}"
            ) from e
        if db.path is None:
            # _memory is always present and never file-backed; only warn about
            # databases this configuration would otherwise try to replicate.
            if db_name != "_memory" and (
                db_config is not None or replica_url_template is not None
            ):
                warnings.append(
                    f"Database '{db_name}' is in-memory only, so Litestream cannot replicate it."
                )
            continue

        db_path = Path(db.path)

        # litestream opens databases read-write and flips them to WAL journal
        # mode, which would physically rewrite a file Datasette promised
        # never to change.
        if not db.is_mutable:
            if db_config is not None:
                raise StartupError(
                    f"datasette-litestream: database '{db_name}' is immutable but "
                    "has a datasette-litestream config block. Litestream would "
                    "rewrite the file (it switches databases to WAL journal "
                    "mode) — open the database as mutable or remove its "
                    "datasette-litestream configuration."
                )
            if replica_url_template is not None:
                warnings.append(
                    f"Database '{db_name}' is immutable, so Litestream will not replicate it."
                )
            continue

        # skip this DB if "replica-url-template" was not defined or no db-level config was given
        if db_config is None and replica_url_template is None:
            continue

        replica_url = resolve_replica_url(
            db_name, db_path, db_config, replica_url_template
        )
        if replica_url is None:
            # Only possible with a db-level block that has no 'replica' URL
            # and no 'replica-url-template' fallback.
            warnings.append(
                f"Database '{db_name}' has a datasette-litestream block but no "
                "'replica' URL, and no top-level 'replica-url-template' is set, so it "
                "will not be replicated."
            )
            continue

        initial.append((db_name, str(db_path.resolve()), replica_url))

    if config.internal_replica_url:
        internal_path, reason = internal_database_path(datasette)
        if internal_path is None:
            warnings.append(f"'internal-replica-url' is set but cannot work: {reason}")
        else:
            # No name: the internal database is identified by its path (and
            # the API's 'internal' flag), never by a reserved database name.
            initial.append(
                (None, str(internal_path.resolve()), config.internal_replica_url)
            )

    for warning in warnings:
        print(f"datasette-litestream: WARNING: {warning}", file=sys.stderr)

    # don't run litestream if no top-level or db-level datasette-litestream config was given
    if not plugin_config_top and len(initial) == 0:
        return

    # Validate every resolved replica URL before the daemon starts: a typo'd
    # scheme should be a clean startup error, not a control-socket traceback
    # after some databases are already replicating. (The register API applies
    # the same check to caller-supplied URLs.)
    def _validated(db_name, db_path, replica_url):
        label = f"database '{db_name}'" if db_name is not None else "internal database"
        try:
            return (db_name, db_path, validate_replica_url(replica_url))
        except ValueError as e:
            raise StartupError(
                f"datasette-litestream: {label}: {e} (URL: {replica_url!r})"
            ) from e

    initial = [_validated(*entry) for entry in initial]

    # Constructed only now that we know the plugin will run: __init__ opens
    # the log destination (possibly the operator's configured log file).
    litestream_process = LitestreamProcess(logging_config=config.logging)
    litestream_process.credentials = creds
    litestream_process.current_credentials_hash = credentials_hash(creds)
    litestream_process.metrics_addr = config.metrics_addr
    litestream_process.warnings = warnings

    startup_id = str(uuid.uuid4())
    litestream_process.startup_id = startup_id
    processes[startup_id] = litestream_process
    setattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, startup_id)

    try:
        litestream_process.start_daemon()
        for db_name, db_path, replica_url in initial:
            try:
                litestream_process.register_db(db_path, replica_url, name=db_name)
            except (LitestreamControlError, httpx.HTTPError, RuntimeError) as e:
                raise StartupError(
                    "datasette-litestream: the litestream daemon rejected "
                    f"database '{db_name or db_path}' ({replica_url}): {e}"
                ) from e
    except BaseException:
        # Datasette aborts startup on the raised error; don't leave a daemon
        # child, an open log handle or a registry entry behind. (stop_daemon
        # is final teardown: it also closes the logfile and prunes the
        # registry entry.)
        litestream_process.stop_daemon()
        raise

    # Supervision runs whenever a daemon does: with dynamic credentials the
    # loop doubles as the refresh loop on that interval; otherwise it ticks
    # at the fixed health interval (crash restart + registration reconcile).
    if config.credentials.uses_dynamic and config.credentials.refresh_interval:
        interval = config.credentials.refresh_interval
    else:
        interval = HEALTH_INTERVAL_SECONDS
    litestream_process._refresh_task = asyncio.create_task(
        health_loop(startup_id, config, interval)
    )
