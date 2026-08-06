"""Resolve which databases replicate where.

Pure helpers that turn plugin configuration (replica URL templates,
``all-replicate``, ``replicate-internal``) into concrete replica URLs and
database paths. No daemon or Datasette request state involved.
"""

import os
from pathlib import Path


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
    lives in memory or in a throwaway temp file, so replicating it is useless.
    """
    if not hasattr(datasette, "get_internal_database"):
        return None, "This Datasette version has no internal database."
    internal_db = datasette.get_internal_database()
    if (
        internal_db.path is None
        or internal_db.is_memory
        or getattr(internal_db, "is_temp_disk", False)
    ):
        return None, (
            "The internal database is in-memory only (or an ephemeral temp file), "
            "so Litestream cannot usefully replicate it. Start Datasette with "
            "--internal /path/to/internal.db to persist it."
        )
    return Path(internal_db.path), None


def resolve_replica_url(db_name, db_path, plugin_config_db, all_replicate):
    """Determine the single replica URL for a database, or None to skip it.

    litestream 0.5 replicates each database to exactly one destination, so we
    resolve a single URL. Precedence:
      1. db-level config ``replica`` (a single URL string)
      2. db-level config ``replicas`` (deprecated list; first entry is used)
      3. top-level ``all-replicate`` template (string, or first entry of a list)
    """
    template = None
    if plugin_config_db:
        if plugin_config_db.get("replica"):
            template = plugin_config_db["replica"]
        elif plugin_config_db.get("replicas"):
            replicas = plugin_config_db["replicas"]
            first = replicas[0]
            template = first.get("url") if isinstance(first, dict) else first
    if template is None and all_replicate is not None:
        if isinstance(all_replicate, (list, tuple)):
            template = all_replicate[0] if all_replicate else None
        else:
            template = all_replicate
    if template is None:
        return None
    return expand_replica_template(template, db_name, db_path)
