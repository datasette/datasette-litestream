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

from datasette import hookimpl
from datasette.permissions import Action
from datasette.utils import StartupError
from datasette_vite import vite_entry
from pydantic import ValidationError

from . import routes  # noqa: F401  (imports register the route handlers)
from .config import LitestreamConfig, get_config, get_database_config
from .process import (
    DATASETTE_LITESTREAM_PROCESS_KEY,
    LitestreamProcess,
    credentials_hash,
    get_dynamic_credentials,
    processes,
)
from .replicas import (
    INTERNAL_DB_NAME,
    expand_replica_template,
    internal_database_path,
    resolve_replica_url,
)
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
            # TODO why is this needed?
            and datasette.plugin_config("datasette-litestream") is not None
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


async def credential_refresh_loop(
    startup_id: str, config: LitestreamConfig, interval_seconds: float
):
    """Background task that periodically checks for credential changes.

    Failures here are never fatal: the daemon keeps replicating with the
    last-known-good credentials (valid until the provider expires them) and
    the loop retries on the next tick. Taking down the whole Datasette
    instance over a replication-credentials hiccup would be strictly worse.
    """
    consecutive_failures = 0
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            litestream_process = processes.get(startup_id)
            if litestream_process is None:
                return  # Process no longer exists

            new_creds = get_dynamic_credentials(config)
            if new_creds is None:
                continue

            new_hash = credentials_hash(new_creds)
            # A daemon left down by an earlier failed restart must be brought
            # back even when the credentials themselves did not change.
            daemon_down = litestream_process.process is None
            if new_hash != litestream_process.current_credentials_hash:
                print(
                    "datasette-litestream: credentials changed, restarting litestream",
                    file=sys.stderr,
                )
                await asyncio.to_thread(
                    litestream_process.restart_with_new_credentials, new_creds
                )
            elif daemon_down:
                print(
                    "datasette-litestream: daemon is not running, restarting litestream",
                    file=sys.stderr,
                )
                await asyncio.to_thread(
                    litestream_process.restart_with_new_credentials, new_creds
                )
            consecutive_failures = 0

        except Exception as e:  # noqa: BLE001 -- never let a refresh failure kill the server
            consecutive_failures += 1
            print(
                f"datasette-litestream: error refreshing credentials "
                f"(consecutive failures: {consecutive_failures}), "
                f"continuing with previous credentials: {e}",
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
    if config.uses_dynamic_credentials:
        try:
            creds = get_dynamic_credentials(config)
        except Exception as e:
            raise StartupError(
                f"datasette-litestream: failed to load initial credentials: {e}"
            ) from e
    else:
        creds = config.static_credentials

    all_replicate = config.all_replicate
    warnings = []

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
                db_config is not None or all_replicate is not None
            ):
                warnings.append(
                    f"Database '{db_name}' is in-memory only, so Litestream cannot replicate it."
                )
            continue

        db_path = Path(db.path)

        # skip this DB if "all-replicate" was not defined or no db-level config was given
        if db_config is None and all_replicate is None:
            continue

        replica_url = resolve_replica_url(db_name, db_path, db_config, all_replicate)
        if replica_url is None:
            continue

        initial.append((db_name, str(db_path.resolve()), replica_url))

    if config.replicate_internal:
        internal_path, reason = internal_database_path(datasette)
        if internal_path is None:
            warnings.append(
                f"'replicate-internal' is enabled but cannot work: {reason}"
            )
        else:
            if isinstance(config.replicate_internal, str):
                replica_url = expand_replica_template(
                    config.replicate_internal, INTERNAL_DB_NAME, internal_path
                )
            else:
                replica_url = resolve_replica_url(
                    INTERNAL_DB_NAME, internal_path, None, all_replicate
                )
            if replica_url is None:
                raise StartupError(
                    "datasette-litestream: 'replicate-internal' needs a replica URL — "
                    "set it to a URL template or define 'all-replicate'"
                )
            initial.append(
                (INTERNAL_DB_NAME, str(internal_path.resolve()), replica_url)
            )

    for warning in warnings:
        print(f"datasette-litestream: WARNING: {warning}", file=sys.stderr)

    # don't run litestream if no top-level or db-level datasette-litestream config was given
    if not plugin_config_top and len(initial) == 0:
        return

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

    litestream_process.start_daemon()
    for db_name, db_path, replica_url in initial:
        litestream_process.register_db(db_path, replica_url, name=db_name)

    # Schedule credential refresh if using dynamic credentials. The interval
    # is re-checked here (the model validator guarantees it) to narrow away None.
    if config.uses_dynamic_credentials and config.credentials_refresh_interval:
        litestream_process._refresh_task = asyncio.create_task(
            credential_refresh_loop(
                startup_id, config, config.credentials_refresh_interval
            )
        )
