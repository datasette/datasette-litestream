from datasette.app import Datasette
from datasette.utils import StartupError
import pytest
import sqlite_utils
from pathlib import Path
import json
import time

from datasette_litestream import (
    load_credentials_from_file,
    load_credentials_from_command,
    get_dynamic_credentials,
    credentials_hash,
    redact_credentials,
    processes,
    DATASETTE_LITESTREAM_PROCESS_KEY,
)

actor_root = {"a": {"id": "root"}}


@pytest.fixture
def students_db_path(tmpdir):
    path = str(tmpdir / "students.db")
    db = sqlite_utils.Database(path)
    db["students"].insert_all(
        [
            {"name": "alex", "age": 10},
            {"name": "brian", "age": 20},
            {"name": "craig", "age": 30, "[weird (column)]": 1},
        ]
    )
    db.execute("create table courses(name text primary key) without rowid")
    db["courses"].insert_all(
        [
            {"name": "MATH 101"},
            {"name": "MATH 102"},
        ]
    )
    return path


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


@pytest.mark.asyncio
async def test_basic_db_level(students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    assert not Path(backup_dir).exists()

    datasette = Datasette(
        [students_db_path],
        config={
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
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
    assert (
        "WARNING: metrics-addr was not defined, so no litestream metrics are available."
        in response.text
    )

    # have to wait a second for litestream to write the first replica
    time.sleep(1)
    assert Path(backup_dir).exists()
    assert (Path(backup_dir) / "generations").exists()


@pytest.mark.asyncio
async def test_metrics(students_db_path):
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    assert not Path(backup_dir).exists()

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {"datasette-litestream": {"metrics-addr": ":9998"}},
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
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


# Tests for credential loading functions


def test_load_credentials_from_file(tmpdir):
    """Test loading credentials from a JSON file."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIATEST123",
                "secret-access-key": "secretkey456",
            }
        ),
        encoding="utf-8",
    )

    result = load_credentials_from_file(str(creds_file))
    assert result["access-key-id"] == "AKIATEST123"
    assert result["secret-access-key"] == "secretkey456"
    assert "session-token" not in result


def test_load_credentials_from_file_with_session_token(tmpdir):
    """Test loading credentials with session token from a JSON file."""
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
    assert result["access-key-id"] == "AKIATEST123"
    assert result["secret-access-key"] == "secretkey456"
    assert result["session-token"] == "sessiontoken789"


def test_load_credentials_from_file_missing_keys(tmpdir):
    """Test error when credentials file is missing required keys."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIATEST123"}), encoding="utf-8"
    )

    with pytest.raises(StartupError, match="must contain"):
        load_credentials_from_file(str(creds_file))


def test_load_credentials_from_file_not_found():
    """Test error when credentials file doesn't exist."""
    with pytest.raises(FileNotFoundError):
        load_credentials_from_file("/nonexistent/path/creds.json")


def test_load_credentials_from_command():
    """Test loading credentials from a CLI command."""
    creds_json = json.dumps(
        {
            "access-key-id": "AKIACMD789",
            "secret-access-key": "cmdsecret012",
        }
    )
    # Use echo to output JSON
    result = load_credentials_from_command(f"echo '{creds_json}'")
    assert result["access-key-id"] == "AKIACMD789"
    assert result["secret-access-key"] == "cmdsecret012"
    assert "session-token" not in result


def test_load_credentials_from_command_with_session_token():
    """Test loading credentials with session token from a CLI command."""
    creds_json = json.dumps(
        {
            "access-key-id": "AKIACMD789",
            "secret-access-key": "cmdsecret012",
            "session-token": "cmdsessiontoken345",
        }
    )
    # Use echo to output JSON
    result = load_credentials_from_command(f"echo '{creds_json}'")
    assert result["access-key-id"] == "AKIACMD789"
    assert result["secret-access-key"] == "cmdsecret012"
    assert result["session-token"] == "cmdsessiontoken345"


def test_load_credentials_from_command_with_script(tmpdir):
    """Test loading credentials from a script file."""
    script = tmpdir / "get_creds.sh"
    creds_json = json.dumps(
        {
            "access-key-id": "AKIASCRIPT",
            "secret-access-key": "scriptsecret",
        }
    )
    script.write_text(f"#!/bin/bash\necho '{creds_json}'", encoding="utf-8")
    script.chmod(0o755)

    result = load_credentials_from_command(str(script))
    assert result["access-key-id"] == "AKIASCRIPT"
    assert result["secret-access-key"] == "scriptsecret"


def test_load_credentials_from_command_failure():
    """Test error when credentials command fails."""
    with pytest.raises(StartupError, match="failed with return code"):
        load_credentials_from_command("false")  # 'false' command always returns 1


def test_load_credentials_from_command_invalid_json():
    """Test error when credentials command outputs invalid JSON."""
    with pytest.raises(StartupError, match="not valid JSON"):
        load_credentials_from_command("echo 'not json'")


def test_load_credentials_from_command_missing_keys():
    """Test error when credentials command output is missing required keys."""
    with pytest.raises(StartupError, match="must contain"):
        load_credentials_from_command('echo \'{"access-key-id": "test"}\'')


def test_get_dynamic_credentials_with_file(tmpdir):
    """Test get_dynamic_credentials with credentials-file option."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIAFILE",
                "secret-access-key": "filesecret",
            }
        ),
        encoding="utf-8",
    )

    result = get_dynamic_credentials({"credentials-file": str(creds_file)})
    assert result["access-key-id"] == "AKIAFILE"
    assert result["secret-access-key"] == "filesecret"


def test_get_dynamic_credentials_with_command():
    """Test get_dynamic_credentials with credentials-command option."""
    creds_json = json.dumps(
        {
            "access-key-id": "AKIACMD",
            "secret-access-key": "cmdsecret",
        }
    )
    result = get_dynamic_credentials({"credentials-command": f"echo '{creds_json}'"})
    assert result["access-key-id"] == "AKIACMD"
    assert result["secret-access-key"] == "cmdsecret"


def test_get_dynamic_credentials_neither():
    """Test get_dynamic_credentials returns None when neither option is set."""
    result = get_dynamic_credentials({})
    assert result is None


def test_get_dynamic_credentials_with_session_token_file(tmpdir):
    """Test get_dynamic_credentials with session token from file."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIAFILE",
                "secret-access-key": "filesecret",
                "session-token": "filesessiontoken",
            }
        ),
        encoding="utf-8",
    )

    result = get_dynamic_credentials({"credentials-file": str(creds_file)})
    assert result["access-key-id"] == "AKIAFILE"
    assert result["secret-access-key"] == "filesecret"
    assert result["session-token"] == "filesessiontoken"


def test_get_dynamic_credentials_with_session_token_command():
    """Test get_dynamic_credentials with session token from command."""
    creds_json = json.dumps(
        {
            "access-key-id": "AKIACMD",
            "secret-access-key": "cmdsecret",
            "session-token": "cmdsessiontoken",
        }
    )
    result = get_dynamic_credentials({"credentials-command": f"echo '{creds_json}'"})
    assert result["access-key-id"] == "AKIACMD"
    assert result["secret-access-key"] == "cmdsecret"
    assert result["session-token"] == "cmdsessiontoken"


def test_credentials_hash():
    """Test credentials hash function."""
    creds1 = {"access-key-id": "key1", "secret-access-key": "secret1"}
    creds2 = {"access-key-id": "key1", "secret-access-key": "secret1"}
    creds3 = {"access-key-id": "key2", "secret-access-key": "secret1"}

    assert credentials_hash(creds1) == credentials_hash(creds2)
    assert credentials_hash(creds1) != credentials_hash(creds3)
    assert credentials_hash(None) == ""


def test_credentials_hash_with_session_token():
    """Test credentials hash includes session token."""
    creds_no_token = {"access-key-id": "key1", "secret-access-key": "secret1"}
    creds_with_token = {
        "access-key-id": "key1",
        "secret-access-key": "secret1",
        "session-token": "token1",
    }
    creds_with_different_token = {
        "access-key-id": "key1",
        "secret-access-key": "secret1",
        "session-token": "token2",
    }

    # Credentials with and without token should have different hashes
    assert credentials_hash(creds_no_token) != credentials_hash(creds_with_token)
    # Different session tokens should produce different hashes
    assert credentials_hash(creds_with_token) != credentials_hash(
        creds_with_different_token
    )
    # Same credentials with same token should have same hash
    creds_with_token_copy = {
        "access-key-id": "key1",
        "secret-access-key": "secret1",
        "session-token": "token1",
    }
    assert credentials_hash(creds_with_token) == credentials_hash(creds_with_token_copy)


# Integration tests for dynamic credentials


@pytest.mark.asyncio
async def test_credentials_file_and_command_error(students_db_path, tmpdir):
    """Test error when both credentials-file and credentials-command are specified."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIATEST",
                "secret-access-key": "secrettest",
            }
        ),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": str(creds_file),
                    "credentials-command": "echo '{}'",
                    "credentials-refresh-interval": 60,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            },
        },
    )

    with pytest.raises(StartupError, match="cannot specify both"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_refresh_interval_required(students_db_path, tmpdir):
    """Test error when credentials-file is used without refresh interval."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIATEST",
                "secret-access-key": "secrettest",
            }
        ),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": str(creds_file),
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            },
        },
    )

    with pytest.raises(StartupError, match="credentials-refresh-interval.*required"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_file_basic(students_db_path, tmpdir):
    """Test basic operation with credentials from file."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIAFILETEST",
                "secret-access-key": "filesecrettest",
            }
        ),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": str(creds_file),
                    "credentials-refresh-interval": 300,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
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

    # have to wait a second for litestream to write the first replica
    time.sleep(1)
    assert Path(backup_dir).exists()


@pytest.mark.asyncio
async def test_credentials_command_basic(students_db_path, tmpdir):
    """Test basic operation with credentials from command."""
    creds_json = json.dumps(
        {
            "access-key-id": "AKIACMDTEST",
            "secret-access-key": "cmdsecrettest",
        }
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-command": f"echo '{creds_json}'",
                    "credentials-refresh-interval": 300,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
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

    # have to wait a second for litestream to write the first replica
    time.sleep(1)
    assert Path(backup_dir).exists()


@pytest.mark.asyncio
async def test_credentials_file_not_found_error(students_db_path):
    """Test error when credentials file doesn't exist."""
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": "/nonexistent/creds.json",
                    "credentials-refresh-interval": 60,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            },
        },
    )

    with pytest.raises(StartupError, match="failed to load initial credentials"):
        await datasette.invoke_startup()


@pytest.mark.asyncio
async def test_credentials_command_failure_at_startup(students_db_path):
    """Test error when credentials command fails at startup."""
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-command": "false",  # always fails
                    "credentials-refresh-interval": 60,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            },
        },
    )

    with pytest.raises(StartupError, match="failed to load initial credentials"):
        await datasette.invoke_startup()


# Tests for credential redaction


def test_redact_credentials_basic():
    """Test that secret-access-key is redacted."""
    config = {
        "access-key-id": "AKIATEST",
        "secret-access-key": "supersecret123",
        "dbs": [],
    }
    result = redact_credentials(config)
    assert result["access-key-id"] == "AKIATEST"
    assert result["secret-access-key"] == "***REDACTED***"
    assert result["dbs"] == []


def test_redact_credentials_with_session_token():
    """Test that session-token is also redacted."""
    config = {
        "access-key-id": "AKIATEST",
        "secret-access-key": "supersecret123",
        "session-token": "sessiontoken456",
        "dbs": [],
    }
    result = redact_credentials(config)
    assert result["access-key-id"] == "AKIATEST"
    assert result["secret-access-key"] == "***REDACTED***"
    assert result["session-token"] == "***REDACTED***"
    assert result["dbs"] == []


def test_redact_credentials_without_secrets():
    """Test redaction when no secrets are present."""
    config = {
        "dbs": [{"path": "/data/db.sqlite"}],
        "addr": ":9999",
    }
    result = redact_credentials(config)
    assert result == config


@pytest.mark.asyncio
async def test_credentials_redacted_in_status_page(students_db_path, tmpdir):
    """Test that secret-access-key and session-token are redacted on the status page."""
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
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": str(creds_file),
                    "credentials-refresh-interval": 300,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
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

    # The access key ID should be visible
    assert "AKIAVISIBLE" in response.text

    # The secret values should NOT be visible
    assert "supersecretvalue789" not in response.text
    assert "sessiontokenvalue123" not in response.text

    # The redacted marker should be visible instead
    assert "***REDACTED***" in response.text


@pytest.mark.asyncio
async def test_credential_refresh_task_is_stored(students_db_path, tmpdir):
    """Test that the credential refresh task is stored to prevent garbage collection.

    This prevents the "Task was destroyed but it is pending!" warning that occurs
    when asyncio.create_task() is called but the returned task is not stored.
    """
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps(
            {
                "access-key-id": "AKIATEST",
                "secret-access-key": "secrettest",
            }
        ),
        encoding="utf-8",
    )
    backup_dir = str(Path(students_db_path).parents[0] / "students-backup")

    datasette = Datasette(
        [students_db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": str(creds_file),
                    "credentials-refresh-interval": 300,
                }
            },
            "databases": {
                "students": {
                    "plugins": {
                        "datasette-litestream": {"replicas": [{"path": backup_dir}]}
                    }
                }
            },
        },
    )

    # Make a request to trigger startup
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200

    # Get the LitestreamProcess instance
    startup_id = getattr(datasette, DATASETTE_LITESTREAM_PROCESS_KEY, None)
    assert startup_id is not None, "Datasette should have a litestream process key"

    litestream_process = processes.get(startup_id)
    assert litestream_process is not None, "LitestreamProcess should exist"

    # The refresh task should be stored on the process to prevent GC
    assert hasattr(
        litestream_process, "_refresh_task"
    ), "LitestreamProcess should have _refresh_task attribute"
    assert (
        litestream_process._refresh_task is not None
    ), "Refresh task should be stored (not None) when using dynamic credentials"

    # The task should be pending (not done)
    assert (
        not litestream_process._refresh_task.done()
    ), "Refresh task should still be running"
