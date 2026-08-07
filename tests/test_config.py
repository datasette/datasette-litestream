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
            "replica-url-template": "s3://bucket/$DB_NAME",
            "internal-replica-url": "s3://bucket/internal",
            "metrics-addr": ":9090",
            "credentials": {"access-key-id": "AKIA", "secret-access-key": "secret"},
        }
    )
    assert config.replica_url_template == "s3://bucket/$DB_NAME"
    assert config.internal_replica_url == "s3://bucket/internal"
    assert config.metrics_addr == ":9090"
    assert config.credentials.access_key_id == "AKIA"


def test_restrict_runtime_replicas_parses():
    assert LitestreamConfig.model_validate({}).restrict_runtime_replicas is False
    config = LitestreamConfig.model_validate({"restrict-runtime-replicas": True})
    assert config.restrict_runtime_replicas is True


def test_unknown_top_level_key_is_forbidden():
    with pytest.raises(ValidationError, match="not-a-real-key"):
        LitestreamConfig.model_validate({"not-a-real-key": 1})
    with pytest.raises(ValidationError, match="replica-templat"):
        LitestreamConfig.model_validate({"replica-templat": "s3://typo"})


def test_snake_case_spellings_are_accepted():
    """populate_by_name means snake_case keys populate the field instead of
    being silently ignored (the pre-model behavior for e.g. replica_url_template)."""
    config = LitestreamConfig.model_validate(
        {"replica_url_template": "s3://bucket/$DB_NAME"}
    )
    assert config.replica_url_template == "s3://bucket/$DB_NAME"


def test_replica_template_list_is_rejected():
    """Pre-0.5 versions accepted a list and used the first entry; litestream
    0.5 replicates to a single destination, so a list is now an error."""
    with pytest.raises(ValidationError, match="single URL template"):
        LitestreamConfig.model_validate(
            {"replica-url-template": ["s3://bucket/a", "s3://bucket/b"]}
        )
    with pytest.raises(ValidationError, match="single URL template"):
        LitestreamConfig.model_validate({"replica-url-template": []})


def test_internal_replica_url_is_a_url():
    assert LitestreamConfig().internal_replica_url is None
    assert (
        LitestreamConfig.model_validate(
            {"internal-replica-url": "s3://bucket/internal"}
        ).internal_replica_url
        == "s3://bucket/internal"
    )


def test_internal_replica_url_rejects_non_strings():
    # The pre-0.5 'replicate-internal: true' form is gone; the key holds a URL.
    with pytest.raises(ValidationError, match="internal-replica-url"):
        LitestreamConfig.model_validate({"internal-replica-url": True})
    with pytest.raises(ValidationError, match="replicate-internal"):
        LitestreamConfig.model_validate({"replicate-internal": True})


def test_credentials_block_static():
    config = LitestreamConfig.model_validate(
        {"credentials": {"access-key-id": "AKIA", "secret-access-key": "secret"}}
    )
    assert config.credentials.static == Credentials(
        access_key_id="AKIA", secret_access_key="secret"
    )
    assert config.credentials.uses_dynamic is False


def test_credentials_block_dynamic():
    config = LitestreamConfig.model_validate(
        {"credentials": {"file": "/creds.json", "refresh-interval": 60}}
    )
    assert config.credentials.file == "/creds.json"
    assert config.credentials.refresh_interval == 60
    assert config.credentials.uses_dynamic is True
    assert config.credentials.static is None


def test_credentials_file_and_command_mutually_exclusive():
    with pytest.raises(ValidationError, match="cannot specify both"):
        LitestreamConfig.model_validate(
            {
                "credentials": {
                    "file": "/creds.json",
                    "command": "cmd",
                    "refresh-interval": 60,
                }
            }
        )


def test_credentials_refresh_interval_required_with_dynamic():
    with pytest.raises(ValidationError, match="refresh-interval.*required"):
        LitestreamConfig.model_validate({"credentials": {"file": "/creds.json"}})


def test_credentials_refresh_interval_alone_is_rejected():
    with pytest.raises(ValidationError, match="refresh-interval.*without"):
        LitestreamConfig.model_validate({"credentials": {"refresh-interval": 60}})


@pytest.mark.parametrize("interval", [-5, 0, 0.5])
def test_credentials_refresh_interval_must_be_at_least_one_second(interval):
    """Zero or negative would turn the refresh loop into a busy loop
    (asyncio.sleep returns immediately); sub-second is close enough to count."""
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        LitestreamConfig.model_validate(
            {"credentials": {"file": "/creds.json", "refresh-interval": interval}}
        )


def test_credentials_static_and_dynamic_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="cannot be combined"):
        LitestreamConfig.model_validate(
            {
                "credentials": {
                    "access-key-id": "AKIA",
                    "file": "/creds.json",
                    "refresh-interval": 60,
                }
            }
        )


def test_unknown_credentials_key_is_forbidden():
    with pytest.raises(ValidationError, match="acces-key-id"):
        LitestreamConfig.model_validate({"credentials": {"acces-key-id": "typo"}})


def test_old_flat_credential_keys_are_rejected():
    """The pre-0.5 flat credential keys were replaced by the ``credentials``
    block; extra="forbid" surfaces them with a clear error."""
    with pytest.raises(ValidationError, match="access-key-id"):
        LitestreamConfig.model_validate({"access-key-id": "AKIA"})
    with pytest.raises(ValidationError, match="credentials-file"):
        LitestreamConfig.model_validate(
            {"credentials-file": "/creds.json", "credentials-refresh-interval": 60}
        )


def test_no_credentials_at_all():
    config = LitestreamConfig()
    assert config.credentials.static is None
    assert config.credentials.uses_dynamic is False


# --- DatabaseConfig ---------------------------------------------------------


def test_database_config_replicas_list_is_rejected():
    """The litestream <= 0.3 multi-replica list is gone; the error carries a
    migration hint instead of a bare extra-keys complaint."""
    with pytest.raises(ValidationError, match="no longer supported"):
        DatabaseConfig.model_validate({"replicas": ["s3://a", "s3://b"]})
    with pytest.raises(ValidationError, match="use 'replica'"):
        DatabaseConfig.model_validate({"replicas": [{"url": "s3://a"}]})
    # Even alongside an explicit 'replica' — the list would be silently dead.
    with pytest.raises(ValidationError, match="no longer supported"):
        DatabaseConfig.model_validate(
            {"replica": "s3://explicit", "replicas": ["s3://a"]}
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
        config={
            "plugins": {"datasette-litestream": {"replica-url-template": "s3://x"}}
        },
    )
    assert get_config(ds) is get_config(ds)
    assert get_config(ds).replica_url_template == "s3://x"


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
