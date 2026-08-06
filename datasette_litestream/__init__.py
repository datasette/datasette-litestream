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

from . import routes  # noqa: F401  (imports register the route handlers)
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
from ._vite import vite_entry


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
    """Expose the Vite entry helper to templates as datasette_litestream_vite_entry."""

    def entry(entrypoint):
        return vite_entry(datasette, entrypoint)

    return {"datasette_litestream_vite_entry": entry}


async def credential_refresh_loop(
    startup_id: str, plugin_config: dict, interval_seconds: int
):
    """Background task that periodically checks for credential changes."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            litestream_process = processes.get(startup_id)
            if litestream_process is None:
                return  # Process no longer exists

            new_creds = get_dynamic_credentials(plugin_config)
            if new_creds is None:
                continue

            new_hash = credentials_hash(new_creds)
            if new_hash != litestream_process.current_credentials_hash:
                print(
                    f"datasette-litestream: credentials changed, restarting litestream",
                    file=sys.stderr,
                )
                await asyncio.to_thread(
                    litestream_process.restart_with_new_credentials, new_creds
                )

        except Exception as e:
            print(
                f"datasette-litestream: fatal error refreshing credentials: {e}",
                file=sys.stderr,
            )
            sys.exit(1)


@hookimpl
def startup(datasette):
    litestream_process = LitestreamProcess()

    plugin_config_top = datasette.plugin_config("datasette-litestream") or {}

    # Validate mutually exclusive credential options
    credentials_file = plugin_config_top.get("credentials-file")
    credentials_command = plugin_config_top.get("credentials-command")
    credentials_refresh_interval = plugin_config_top.get("credentials-refresh-interval")

    if credentials_file and credentials_command:
        raise StartupError(
            "datasette-litestream: cannot specify both 'credentials-file' and 'credentials-command'"
        )

    uses_dynamic_credentials = credentials_file or credentials_command

    if uses_dynamic_credentials and not credentials_refresh_interval:
        raise StartupError(
            "datasette-litestream: 'credentials-refresh-interval' is required when using "
            "'credentials-file' or 'credentials-command'"
        )

    # Load credentials from file/command or from static config
    creds = {}
    if uses_dynamic_credentials:
        try:
            dynamic_creds = get_dynamic_credentials(plugin_config_top)
            creds = dynamic_creds
        except Exception as e:
            raise StartupError(
                f"datasette-litestream: failed to load initial credentials: {e}"
            ) from e
    else:
        if "access-key-id" in plugin_config_top:
            creds["access-key-id"] = plugin_config_top.get("access-key-id")
        if "secret-access-key" in plugin_config_top:
            creds["secret-access-key"] = plugin_config_top.get("secret-access-key")
        if "session-token" in plugin_config_top:
            creds["session-token"] = plugin_config_top.get("session-token")

    litestream_process.credentials = creds or None
    litestream_process.current_credentials_hash = credentials_hash(creds or None)

    if "metrics-addr" in plugin_config_top:
        litestream_process.metrics_addr = plugin_config_top.get("metrics-addr")

    all_replicate = plugin_config_top.get("all-replicate")
    replicate_internal = plugin_config_top.get("replicate-internal")
    warnings = []

    # Work out which databases to replicate at startup.
    initial = []  # list of (db_path_str, replica_url)
    for db_name, db in datasette.databases.items():
        plugin_config_db = datasette.plugin_config(
            "datasette-litestream", db_name, fallback=False
        )
        if db.path is None:
            # _memory is always present and never file-backed; only warn about
            # databases this configuration would otherwise try to replicate.
            if db_name != "_memory" and (
                plugin_config_db is not None or all_replicate is not None
            ):
                warnings.append(
                    f"Database '{db_name}' is in-memory only, so Litestream cannot replicate it."
                )
            continue

        db_path = Path(db.path)

        # skip this DB if "all-replicate" was not defined or no db-level config was given
        if plugin_config_db is None and all_replicate is None:
            continue

        replica_url = resolve_replica_url(
            db_name, db_path, plugin_config_db, all_replicate
        )
        if replica_url is None:
            continue

        initial.append((str(db_path.resolve()), replica_url))

    if replicate_internal:
        internal_path, reason = internal_database_path(datasette)
        if internal_path is None:
            warnings.append(f"'replicate-internal' is enabled but cannot work: {reason}")
        else:
            if isinstance(replicate_internal, str):
                replica_url = expand_replica_template(
                    replicate_internal, INTERNAL_DB_NAME, internal_path
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
            initial.append((str(internal_path.resolve()), replica_url))

    litestream_process.warnings = warnings
    for warning in warnings:
        print(f"datasette-litestream: WARNING: {warning}", file=sys.stderr)

    # don't run litestream if no top-level or db-level datasette-litestream config was given
    if not plugin_config_top and len(initial) == 0:
        return

    startup_id = str(uuid.uuid4())
    processes[startup_id] = litestream_process
    setattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, startup_id)

    litestream_process.start_daemon()
    for db_path, replica_url in initial:
        litestream_process.register_db(db_path, replica_url)

    # Schedule credential refresh if using dynamic credentials. The interval
    # is re-checked here (validated non-empty above) to narrow away None.
    if uses_dynamic_credentials and credentials_refresh_interval:
        litestream_process._refresh_task = asyncio.create_task(
            credential_refresh_loop(
                startup_id, plugin_config_top, credentials_refresh_interval
            )
        )
