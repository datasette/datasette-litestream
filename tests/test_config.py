"""Tests for the typed plugin configuration (datasette_litestream/config.py):
model validation, kebab-case aliases, extra="forbid" typo detection, union
normalization, and the startup behavior for invalid config."""

import pytest
from conftest import table
from datasette.app import Datasette
from datasette.utils import StartupError
from pydantic import ValidationError

from datasette_litestream.config import (
    Credentials,
    DatabaseConfig,
    LitestreamConfig,
    LoadedCredentials,
    get_config,
    get_database_config,
)

# --- LitestreamConfig -------------------------------------------------------


def test_top_level_kebab_aliases():
    config = LitestreamConfig.model_validate(
        {
            "all-replicate": "s3://bucket/$DB_NAME",
            "replicate-internal": True,
            "metrics-addr": ":9090",
            "access-key-id": "AKIA",
            "secret-access-key": "secret",
        }
    )
    assert config.all_replicate == "s3://bucket/$DB_NAME"
    assert config.replicate_internal is True
    assert config.metrics_addr == ":9090"
    assert config.access_key_id == "AKIA"


def test_restrict_runtime_replicas_parses():
    assert LitestreamConfig.model_validate({}).restrict_runtime_replicas is False
    config = LitestreamConfig.model_validate({"restrict-runtime-replicas": True})
    assert config.restrict_runtime_replicas is True


def test_unknown_top_level_key_is_forbidden():
    with pytest.raises(ValidationError, match="not-a-real-key"):
        LitestreamConfig.model_validate({"not-a-real-key": 1})
    with pytest.raises(ValidationError, match="all-replicat"):
        LitestreamConfig.model_validate({"all-replicat": "s3://typo"})


def test_snake_case_spellings_are_accepted():
    """populate_by_name means snake_case keys populate the field instead of
    being silently ignored (the pre-model behavior for e.g. all_replicate)."""
    config = LitestreamConfig.model_validate({"all_replicate": "s3://bucket/$DB_NAME"})
    assert config.all_replicate == "s3://bucket/$DB_NAME"


def test_all_replicate_list_uses_first_entry():
    config = LitestreamConfig.model_validate(
        {"all-replicate": ["s3://bucket/a", "s3://bucket/b"]}
    )
    assert config.all_replicate == "s3://bucket/a"
    assert LitestreamConfig.model_validate({"all-replicate": []}).all_replicate is None


def test_replicate_internal_accepts_bool_or_template():
    assert LitestreamConfig().replicate_internal is False
    assert (
        LitestreamConfig.model_validate(
            {"replicate-internal": "s3://bucket/internal"}
        ).replicate_internal
        == "s3://bucket/internal"
    )


def test_credentials_file_and_command_mutually_exclusive():
    with pytest.raises(ValidationError, match="cannot specify both"):
        LitestreamConfig.model_validate(
            {
                "credentials-file": "/creds.json",
                "credentials-command": "cmd",
                "credentials-refresh-interval": 60,
            }
        )


def test_credentials_refresh_interval_required_with_dynamic():
    with pytest.raises(ValidationError, match="credentials-refresh-interval.*required"):
        LitestreamConfig.model_validate({"credentials-file": "/creds.json"})


def test_static_credentials():
    assert LitestreamConfig().static_credentials is None
    creds = LitestreamConfig.model_validate(
        {"access-key-id": "AKIA", "secret-access-key": "secret"}
    ).static_credentials
    assert creds == Credentials(access_key_id="AKIA", secret_access_key="secret")


# --- DatabaseConfig ---------------------------------------------------------


def test_database_config_replicas_deprecated_folds_into_replica():
    assert (
        DatabaseConfig.model_validate({"replicas": ["s3://a", "s3://b"]}).replica
        == "s3://a"
    )
    assert (
        DatabaseConfig.model_validate({"replicas": [{"url": "s3://a"}]}).replica
        == "s3://a"
    )
    # An explicit replica wins over the deprecated list.
    assert (
        DatabaseConfig.model_validate(
            {"replica": "s3://explicit", "replicas": ["s3://a"]}
        ).replica
        == "s3://explicit"
    )


def test_unknown_database_key_is_forbidden():
    with pytest.raises(ValidationError, match="replika"):
        DatabaseConfig.model_validate({"replika": "s3://typo"})


# --- Credentials ------------------------------------------------------------


def test_loaded_credentials_requires_key_pair():
    with pytest.raises(ValidationError):
        LoadedCredentials.model_validate({"access-key-id": "AKIA"})


def test_credentials_ignore_extra_keys():
    # Credential commands often emit metadata (e.g. an STS expiration).
    creds = LoadedCredentials.model_validate(
        {
            "access-key-id": "AKIA",
            "secret-access-key": "secret",
            "expiration": "2026-01-01T00:00:00Z",
        }
    )
    assert creds.access_key_id == "AKIA"


# --- Datasette integration --------------------------------------------------


@pytest.mark.asyncio
async def test_startup_rejects_unknown_top_level_key(tmpdir):
    ds = Datasette(
        memory=True,
        config={"plugins": {"datasette-litestream": {"all-replicat": "s3://x"}}},
    )
    with pytest.raises(StartupError, match="invalid configuration"):
        await ds.invoke_startup()


@pytest.mark.asyncio
async def test_startup_rejects_unknown_database_key(tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    ds = Datasette(
        [db_path],
        config={
            "databases": {
                "data": {"plugins": {"datasette-litestream": {"replika": "s3://typo"}}}
            }
        },
    )
    with pytest.raises(StartupError, match="database 'data'"):
        await ds.invoke_startup()


@pytest.mark.asyncio
async def test_get_config_is_cached_per_instance():
    ds = Datasette(
        memory=True,
        config={"plugins": {"datasette-litestream": {"all-replicate": "s3://x"}}},
    )
    assert get_config(ds) is get_config(ds)
    assert get_config(ds).all_replicate == "s3://x"


@pytest.mark.asyncio
async def test_get_database_config_none_vs_present(tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    ds = Datasette(
        [db_path],
        config={"databases": {"data": {"plugins": {"datasette-litestream": {}}}}},
    )
    # Presence of an (even empty) db-level block is meaningful.
    assert get_database_config(ds, "data") == DatabaseConfig()
    assert get_database_config(ds, "other") is None
