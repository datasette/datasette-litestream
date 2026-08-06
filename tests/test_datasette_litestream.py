import asyncio
import json
from pathlib import Path

import pytest
import sqlite_utils
from conftest import table
from datasette.app import Datasette
from datasette.database import Database
from datasette.utils import StartupError
from pydantic import ValidationError

from datasette_litestream.config import (
    Credentials,
    DatabaseConfig,
    LitestreamConfig,
    LoggingConfig,
)
from datasette_litestream.process import (
    DATASETTE_LITESTREAM_PROCESS_KEY,
    LitestreamProcess,
    credentials_env,
    credentials_hash,
    get_dynamic_credentials,
    get_process,
    load_credentials_from_command,
    load_credentials_from_file,
    processes,
    redact_credentials,
)
from datasette_litestream.replicas import (
    expand_replica_template,
    resolve_replica_url,
)

actor_root = {"a": {"id": "root"}}


@pytest.fixture
def students_db_path(tmpdir):
    path = str(tmpdir / "students.db")
    db = sqlite_utils.Database(path)
    table(path, "students").insert_all(
        [
            {"name": "alex", "age": 10},
            {"name": "brian", "age": 20},
            {"name": "craig", "age": 30, "[weird (column)]": 1},
        ]
    )
    db.execute("create table courses(name text primary key) without rowid")
    table(path, "courses").insert_all(
        [
            {"name": "MATH 101"},
            {"name": "MATH 102"},
        ]
    )
    return path


def file_replica(path) -> str:
    """A file:// replica URL for a backup directory."""
    return "file://" + str(path)


def replica_has_data(backup_dir) -> bool:
    """litestream 0.5 writes LTX files under <replica>/ltx/."""
    return (Path(backup_dir) / "ltx").exists()


async def root_token(datasette) -> dict:
    """Authorization header for a root bearer token."""
    token = await datasette.create_token("root")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Basic plugin wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-litestream" in installed_plugins


@pytest.mark.asyncio
async def test_no_litestream_config():
    datasette = Datasette(memory=True)
    datasette.root_enabled = True

    response = await datasette.client.get("/-/litestream-status")
    assert response.status_code == 403

    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    assert response.text == "<h1>Litestream not running</h1>"


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------


def test_credentials_env_basic():
    env = credentials_env(
        Credentials.model_validate(
            {"access-key-id": "AKIA", "secret-access-key": "secret"}
        )
    )
    assert env == {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret"}


def test_credentials_env_with_session_token():
    env = credentials_env(
        Credentials.model_validate(
            {
                "access-key-id": "AKIA",
                "secret-access-key": "secret",
                "session-token": "token",
            }
        )
    )
    assert env["AWS_SESSION_TOKEN"] == "token"


def test_credentials_env_empty():
    assert credentials_env(None) == {}
    assert credentials_env(Credentials()) == {}


def test_expand_replica_template(tmpdir):
    db_path = Path(str(tmpdir / "mydb.db"))
    assert (
        expand_replica_template("file:///backups/$DB_NAME", "mydb", db_path)
        == "file:///backups/mydb"
    )
    result = expand_replica_template("s3://bucket/$DB_DIRECTORY", "mydb", db_path)
    assert str(tmpdir) in result


def test_resolve_replica_url_db_level_single(tmpdir):
    db_path = Path(str(tmpdir / "mydb.db"))
    url = resolve_replica_url(
        "mydb", db_path, DatabaseConfig(replica="s3://bucket/mydb"), None
    )
    assert url == "s3://bucket/mydb"


def test_resolve_replica_url_db_level_replicas_list(tmpdir):
    """The deprecated 'replicas' list uses the first entry."""
    db_path = Path(str(tmpdir / "mydb.db"))
    url = resolve_replica_url(
        "mydb",
        db_path,
        DatabaseConfig.model_validate(
            {"replicas": [{"url": "s3://bucket/a"}, {"url": "s3://bucket/b"}]}
        ),
        None,
    )
    assert url == "s3://bucket/a"


def test_resolve_replica_url_all_replicate_template(tmpdir):
    db_path = Path(str(tmpdir / "mydb.db"))
    url = resolve_replica_url("mydb", db_path, None, "file:///backups/$DB_NAME")
    assert url == "file:///backups/mydb"


def test_resolve_replica_url_db_level_wins_over_all_replicate(tmpdir):
    db_path = Path(str(tmpdir / "mydb.db"))
    url = resolve_replica_url(
        "mydb",
        db_path,
        DatabaseConfig(replica="s3://specific/mydb"),
        "file:///backups/$DB_NAME",
    )
    assert url == "s3://specific/mydb"


def test_resolve_replica_url_none(tmpdir):
    db_path = Path(str(tmpdir / "mydb.db"))
    assert resolve_replica_url("mydb", db_path, None, None) is None
    assert resolve_replica_url("mydb", db_path, DatabaseConfig(), None) is None


def test_logging_config_defaults():
    config = LitestreamConfig()
    assert config.logging.level == "info"
    assert config.logging.type == "text"
    assert config.logging.path is None


def test_logging_config_parses():
    config = LitestreamConfig.model_validate(
        {"logging": {"level": "warn", "type": "json", "path": "/logs/litestream.log"}}
    )
    assert config.logging.level == "warn"
    assert config.logging.type == "json"
    assert config.logging.path == "/logs/litestream.log"


def test_logging_config_rejects_invalid():
    with pytest.raises(ValidationError):
        LitestreamConfig.model_validate({"logging": {"level": "verbose"}})
    with pytest.raises(ValidationError):
        LitestreamConfig.model_validate({"logging": {"file": "x.log"}})


def test_logging_path_unwritable():
    with pytest.raises(StartupError):
        LitestreamProcess(
            logging_config=LoggingConfig(path="/nonexistent-dir/litestream.log")
        )


# ---------------------------------------------------------------------------
# Integration: startup replication via the control socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_db_level(litestream_binary, students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    assert not Path(backup_dir).exists()

    datasette = Datasette(
        [students_db_path],
        config={
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replica": file_replica(backup_dir)}
                    }
                }
            }
        },
    )
    datasette.root_enabled = True

    response = await datasette.client.get("/-/litestream-status")
    assert response.status_code == 403

    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    assert "<title>Litestream status</title>" in response.text
    # the status page lists the managed database
    assert "students" in response.text

    for _ in range(20):
        if replica_has_data(backup_dir):
            break
        await asyncio.sleep(0.25)
    assert replica_has_data(backup_dir)


@pytest.mark.asyncio
async def test_all_replicate_template(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"]
                }
            }
        },
    )
    await datasette.invoke_startup()

    expected = backups / "data"
    for _ in range(20):
        if replica_has_data(str(expected)):
            break
        await asyncio.sleep(0.25)
    assert replica_has_data(str(expected))


@pytest.mark.asyncio
async def test_logging_path(litestream_binary, tmpdir):
    """litestream's log output is captured in the configured log file."""
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"
    log_path = Path(str(tmpdir / "litestream.log"))

    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": "file://" + str(backups) + "/$DB_NAME",
                    "logging": {"path": str(log_path)},
                }
            }
        },
    )
    await datasette.invoke_startup()

    process = get_process(datasette)
    assert process.daemon_config["logging"] == {
        "level": "info",
        "type": "text",
        "stderr": True,
    }

    for _ in range(20):
        if replica_has_data(str(backups / "data")):
            break
        await asyncio.sleep(0.25)
    assert replica_has_data(str(backups / "data"))

    assert "level=INFO" in log_path.read_text()


# ---------------------------------------------------------------------------
# Integration: runtime add/remove over the control socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_register_and_unregister(litestream_binary, tmpdir):
    data_path = str(tmpdir / "data.db")
    extra_path = str(tmpdir / "extra.db")
    table(data_path, "t").insert({"v": 1})
    table(extra_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    datasette = Datasette(
        [data_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"]
                }
            }
        },
    )
    datasette.root_enabled = True
    await datasette.invoke_startup()

    headers = await root_token(datasette)
    proc = processes[getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY)]

    # "data" registered at startup; "extra" not attached yet.
    assert any("data.db" in p for p in proc.registered)
    assert not any("extra.db" in p for p in proc.registered)

    # Attach "extra" to Datasette at runtime, then register it for replication.
    datasette.add_database(
        Database(datasette, path=extra_path, is_mutable=True), name="extra"
    )

    response = await datasette.client.post(
        "/-/litestream/register", json={"database": "extra"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] in ("registered", "already_registered")
    assert any("extra.db" in p for p in proc.registered)

    # The daemon's /list now reports both databases on the status page.
    listed = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert "extra.db" in listed.text

    extra_backup = backups / "extra"
    for _ in range(20):
        if replica_has_data(str(extra_backup)):
            break
        await asyncio.sleep(0.25)
    assert replica_has_data(str(extra_backup))

    # Now unregister it.
    response = await datasette.client.post(
        "/-/litestream/unregister", json={"database": "extra"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] in ("unregistered", "already_unregistered")
    assert not any("extra.db" in p for p in proc.registered)


@pytest.mark.asyncio
async def test_register_route_requires_permission(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"]
                }
            }
        },
    )
    await datasette.invoke_startup()

    response = await datasette.client.post(
        "/-/litestream/register", json={"database": "data"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_register_unknown_database(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"]
                }
            }
        },
    )
    datasette.root_enabled = True
    await datasette.invoke_startup()
    headers = await root_token(datasette)

    response = await datasette.client.post(
        "/-/litestream/register",
        json={"database": "does-not-exist"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["ok"] is False


@pytest.mark.asyncio
async def test_metrics(litestream_binary, students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {"datasette-litestream": {"metrics-addr": ":9998"}},
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replica": file_replica(backup_dir)}
                    }
                }
            },
        },
    )
    datasette.root_enabled = True

    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    assert "<title>Litestream status</title>" in response.text
    assert "<h2>Metrics</h2>" in response.text


# ---------------------------------------------------------------------------
# Credential loading helpers (unchanged behavior)
# ---------------------------------------------------------------------------


def test_load_credentials_from_file(tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {"access-key-id": "AKIATEST123", "secret-access-key": "secretkey456"}
        ),
        encoding="utf-8",
    )
    result = load_credentials_from_file(str(creds_file))
    assert result.access_key_id == "AKIATEST123"
    assert result.secret_access_key == "secretkey456"
    assert result.session_token is None


def test_load_credentials_from_file_with_session_token(tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIATEST123",
                "secret-access-key": "secretkey456",
                "session-token": "sessiontoken789",
            }
        ),
        encoding="utf-8",
    )
    result = load_credentials_from_file(str(creds_file))
    assert result.session_token == "sessiontoken789"


def test_load_credentials_from_file_missing_keys(tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIATEST123"}), encoding="utf-8"
    )
    with pytest.raises(StartupError, match="must contain"):
        load_credentials_from_file(str(creds_file))


def test_load_credentials_from_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_credentials_from_file("/nonexistent/path/creds.json")


def test_load_credentials_from_command():
    creds_json = json.dumps(
        {"access-key-id": "AKIACMD789", "secret-access-key": "cmdsecret012"}
    )
    result = load_credentials_from_command(f"echo '{creds_json}'")
    assert result.access_key_id == "AKIACMD789"
    assert result.secret_access_key == "cmdsecret012"
    assert result.session_token is None


def test_load_credentials_from_command_with_session_token():
    creds_json = json.dumps(
        {
            "access-key-id": "AKIACMD789",
            "secret-access-key": "cmdsecret012",
            "session-token": "cmdsessiontoken345",
        }
    )
    result = load_credentials_from_command(f"echo '{creds_json}'")
    assert result.session_token == "cmdsessiontoken345"


def test_load_credentials_from_command_failure():
    with pytest.raises(StartupError, match="failed with return code"):
        load_credentials_from_command("false")


def test_load_credentials_from_command_invalid_json():
    with pytest.raises(StartupError, match="not valid JSON"):
        load_credentials_from_command("echo 'not json'")


def test_load_credentials_from_command_missing_keys():
    with pytest.raises(StartupError, match="must contain"):
        load_credentials_from_command('echo \'{"access-key-id": "test"}\'')


def test_get_dynamic_credentials_with_file(tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIAFILE", "secret-access-key": "filesecret"}),
        encoding="utf-8",
    )
    result = get_dynamic_credentials(
        LitestreamConfig(
            credentials_file=str(creds_file), credentials_refresh_interval=60
        )
    )
    assert result is not None
    assert result.access_key_id == "AKIAFILE"


def test_get_dynamic_credentials_with_command():
    creds_json = json.dumps(
        {"access-key-id": "AKIACMD", "secret-access-key": "cmdsecret"}
    )
    result = get_dynamic_credentials(
        LitestreamConfig(
            credentials_command=f"echo '{creds_json}'",
            credentials_refresh_interval=60,
        )
    )
    assert result is not None
    assert result.access_key_id == "AKIACMD"


def test_get_dynamic_credentials_neither():
    assert get_dynamic_credentials(LitestreamConfig()) is None


def test_credentials_hash():
    creds1 = Credentials(access_key_id="key1", secret_access_key="secret1")
    creds2 = Credentials(access_key_id="key1", secret_access_key="secret1")
    creds3 = Credentials(access_key_id="key2", secret_access_key="secret1")
    assert credentials_hash(creds1) == credentials_hash(creds2)
    assert credentials_hash(creds1) != credentials_hash(creds3)
    assert credentials_hash(None) == ""


def test_credentials_hash_with_session_token():
    creds_no_token = Credentials(access_key_id="key1", secret_access_key="secret1")
    creds_with_token = Credentials(
        access_key_id="key1", secret_access_key="secret1", session_token="token1"
    )
    assert credentials_hash(creds_no_token) != credentials_hash(creds_with_token)


# ---------------------------------------------------------------------------
# Credential validation at startup
# ---------------------------------------------------------------------------


def _creds_config(extra, students_backup):
    return {
        "plugins": {"datasette-litestream": extra},
        "databases": {
            "students": {
                "plugins": {
                    "datasette-litestream": {"replica": file_replica(students_backup)}
                }
            }
        },
    }


@pytest.mark.asyncio
async def test_credentials_file_and_command_error(students_db_path, tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIATEST", "secret-access-key": "secrettest"}),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config(
            {
                "credentials-file": str(creds_file),
                "credentials-command": "echo '{}'",
                "credentials-refresh-interval": 60,
            },
            backup_dir,
        ),
    )
    with pytest.raises(StartupError, match="cannot specify both"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_refresh_interval_required(students_db_path, tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIATEST", "secret-access-key": "secrettest"}),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config({"credentials-file": str(creds_file)}, backup_dir),
    )
    with pytest.raises(StartupError, match="credentials-refresh-interval.*required"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_file_not_found_error(students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config(
            {
                "credentials-file": "/nonexistent/creds.json",
                "credentials-refresh-interval": 60,
            },
            backup_dir,
        ),
    )
    with pytest.raises(StartupError, match="failed to load initial credentials"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_command_failure_at_startup(students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config(
            {"credentials-command": "false", "credentials-refresh-interval": 60},
            backup_dir,
        ),
    )
    with pytest.raises(StartupError, match="failed to load initial credentials"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_file_basic(litestream_binary, students_db_path, tmpdir):
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {"access-key-id": "AKIAFILETEST", "secret-access-key": "filesecrettest"}
        ),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config(
            {
                "credentials-file": str(creds_file),
                "credentials-refresh-interval": 300,
            },
            backup_dir,
        ),
    )
    datasette.root_enabled = True
    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    for _ in range(20):
        if replica_has_data(backup_dir):
            break
        await asyncio.sleep(0.25)
    assert replica_has_data(backup_dir)


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------


def test_redact_credentials_basic():
    config = {
        "access-key-id": "AKIATEST",
        "secret-access-key": "supersecret123",
        "dbs": [],
    }
    result = redact_credentials(config)
    assert result["access-key-id"] == "AKIATEST"
    assert result["secret-access-key"] == "***REDACTED***"


def test_redact_credentials_with_session_token():
    config = {
        "access-key-id": "AKIATEST",
        "secret-access-key": "supersecret123",
        "session-token": "sessiontoken456",
    }
    result = redact_credentials(config)
    assert result["secret-access-key"] == "***REDACTED***"
    assert result["session-token"] == "***REDACTED***"


def test_redact_credentials_without_secrets():
    config = {"dbs": [{"path": "/data/db.sqlite"}], "addr": ":9999"}
    assert redact_credentials(config) == config


@pytest.mark.asyncio
async def test_credentials_not_leaked_in_status_page(
    litestream_binary, students_db_path, tmpdir
):
    """Credentials reach litestream via the environment, never the config file,
    so the status page must not contain the secret values."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIAVISIBLE",
                "secret-access-key": "supersecretvalue789",
                "session-token": "sessiontokenvalue123",
            }
        ),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config(
            {
                "credentials-file": str(creds_file),
                "credentials-refresh-interval": 300,
            },
            backup_dir,
        ),
    )
    datasette.root_enabled = True
    response = await datasette.client.get(
        "/-/litestream-status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    assert "supersecretvalue789" not in response.text
    assert "sessiontokenvalue123" not in response.text


@pytest.mark.asyncio
async def test_credential_refresh_task_is_stored(
    litestream_binary, students_db_path, tmpdir
):
    """The credential refresh task must be stored to prevent garbage collection."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIATEST", "secret-access-key": "secrettest"}),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")
    datasette = Datasette(
        [students_db_path],
        config=_creds_config(
            {
                "credentials-file": str(creds_file),
                "credentials-refresh-interval": 300,
            },
            backup_dir,
        ),
    )
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200

    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, None)
    assert startup_id is not None
    litestream_process = processes.get(startup_id)
    assert litestream_process is not None
    assert litestream_process._refresh_task is not None
    assert not litestream_process._refresh_task.done()


# ---------------------------------------------------------------------------
# Internal database replication and in-memory warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replicate_internal(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    datasette = Datasette(
        [db_path],
        internal=str(tmpdir / "internal.db"),
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"],
                    "replicate-internal": True,
                }
            }
        },
    )
    datasette.root_enabled = True
    await datasette.invoke_startup()

    expected = backups / "_internal"
    for _ in range(20):
        if replica_has_data(str(expected)):
            break
        await asyncio.sleep(0.25)
    assert replica_has_data(str(expected))

    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY)
    assert processes[startup_id].warnings == []

    # the status API reports the internal database under its reserved name
    response = await datasette.client.get(
        "/-/litestream/api/status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.status_code == 200
    status = response.json()
    assert "_internal" in [db["database"] for db in status["databases"]]
    assert status["warnings"] == []


@pytest.mark.asyncio
async def test_replicate_internal_ephemeral_warns(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    # No internal= argument: the internal database is an ephemeral temp file
    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"],
                    "replicate-internal": True,
                }
            }
        },
    )
    datasette.root_enabled = True
    await datasette.invoke_startup()

    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY)
    warnings = processes[startup_id].warnings
    assert len(warnings) == 1
    assert "replicate-internal" in warnings[0]

    response = await datasette.client.get(
        "/-/litestream/api/status",
        cookies={"ds_actor": datasette.sign(actor_root, "actor")},
    )
    assert response.json()["warnings"] == warnings


@pytest.mark.asyncio
async def test_in_memory_database_warns(litestream_binary, tmpdir):
    db_path = str(tmpdir / "data.db")
    table(db_path, "t").insert({"v": 1})
    backups = tmpdir / "backups"

    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "all-replicate": ["file://" + str(backups) + "/$DB_NAME"]
                }
            }
        },
    )
    datasette.add_memory_database("scratch")
    await datasette.invoke_startup()

    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY)
    warnings = processes[startup_id].warnings
    assert warnings == [
        "Database 'scratch' is in-memory only, so Litestream cannot replicate it."
    ]
