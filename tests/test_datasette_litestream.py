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


def test_load_credentials_from_file_missing_keys(tmpdir):
    """Test error when credentials file is missing required keys."""
    creds_file = tmpdir / "creds.json"
    creds_file.write_text(
        json.dumps({"access-key-id": "AKIATEST123"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="must contain"):
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
    with pytest.raises(ValueError, match="not valid JSON"):
        load_credentials_from_command("echo 'not json'")


def test_load_credentials_from_command_missing_keys():
    """Test error when credentials command output is missing required keys."""
    with pytest.raises(ValueError, match="must contain"):
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


def test_credentials_hash():
    """Test credentials hash function."""
    creds1 = {"access-key-id": "key1", "secret-access-key": "secret1"}
    creds2 = {"access-key-id": "key1", "secret-access-key": "secret1"}
    creds3 = {"access-key-id": "key2", "secret-access-key": "secret1"}

    assert credentials_hash(creds1) == credentials_hash(creds2)
    assert credentials_hash(creds1) != credentials_hash(creds3)
    assert credentials_hash(None) == ""


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

    with pytest.raises(ValueError, match="cannot specify both"):
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

    with pytest.raises(ValueError, match="credentials-refresh-interval.*required"):
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
