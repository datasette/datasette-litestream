"""Resolve which databases replicate where.

Pure helpers that turn plugin configuration (replica URL templates,
``all-replicate``, ``replicate-internal``) into concrete replica URLs and
database paths. No daemon or Datasette request state involved.
"""

import os
from pathlib import Path

from .config import DatabaseConfig


def expand_replica_template(template: str, db_name: str, db_path: Path) -> str:
    """Expand the $DB_NAME / $DB_DIRECTORY / $PWD variables in a replica URL."""
    return (
        template.replace("$DB_NAME", db_name)
        .replace("$DB_DIRECTORY", str(Path(db_path).resolve().parent))
        .replace("$PWD", os.getcwd())
    )


# Name used for Datasette's internal database in replica URL templates, the
# admin UI and the manage API. Leading underscore avoids clashing with attached
# databases (Datasette reserves underscore-prefixed names).
INTERNAL_DB_NAME = "_internal"


def internal_database_path(datasette):
    """Return (path, None) for a persistent internal database, or (None, reason).

    Without ``datasette --internal /path/to/internal.db`` the internal database
    lives in a throwaway temp file, so replicating it is useless.
    """
    internal_db = datasette.get_internal_database()
    if internal_db.is_temp_disk or internal_db.path is None:
        return None, (
            "The internal database is an ephemeral temp file, so Litestream "
            "cannot usefully replicate it. Start Datasette with "
            "--internal /path/to/internal.db to persist it."
        )
    return Path(internal_db.path), None


def resolve_replica_url(
    db_name,
    db_path,
    db_config: DatabaseConfig | None,
    all_replicate: str | None,
):
    """Determine the single replica URL for a database, or None to skip it.

    litestream 0.5 replicates each database to exactly one destination, so we
    resolve a single URL: the db-level ``replica``, falling back to the
    top-level ``all-replicate`` template.
    """
    template = db_config.replica if db_config is not None else None
    if not template:
        template = all_replicate
    if not template:
        return None
    return expand_replica_template(template, db_name, db_path)
