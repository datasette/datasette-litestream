"""End-to-end credential rotation against a real S3 gateway (versitygw).

Opt-in: not part of the default pytest run. Requires ``versitygw`` and
litestream >= 0.5 on PATH, then:

    just test-versitygw

The scenario: replication works with userA's credentials; userA is deleted
server-side and replication starts failing; the credentials file is rotated
to userB, the refresh loop restarts the daemon, and replication recovers —
including rows written while credentials were dead.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time

import httpx
import pytest
import sqlite_utils
from conftest import litestream_binary_path, table
from datasette.app import Datasette
from versitygw import VersityGateway

from datasette_litestream._client import LitestreamControlError
from datasette_litestream.process import get_process

if not os.environ.get("VERSITYGW_TESTS"):
    pytest.skip(
        "set VERSITYGW_TESTS=1 to run versitygw integration tests",
        allow_module_level=True,
    )

BUCKET = "replicas"


@pytest.fixture
def gateway(tmp_path):
    # These tests are opt-in; missing prerequisites are a loud failure, not
    # a silent skip.
    if shutil.which("versitygw") is None:
        pytest.fail("VERSITYGW_TESTS=1 is set but versitygw is not on PATH")
    if litestream_binary_path() is None:
        pytest.fail(
            "VERSITYGW_TESTS=1 is set but no litestream binary is available; "
            "set LITESTREAM_TEST_BINARY or put litestream >= 0.5 on PATH"
        )
    gw = VersityGateway(tmp_path / "versitygw")
    gw.start()
    yield gw
    gw.stop()


def write_credentials(path, access_key, secret_key):
    path.write_text(
        json.dumps({"access-key-id": access_key, "secret-access-key": secret_key}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_credential_rotation_end_to_end(gateway, tmp_path):
    gateway.create_user("userA", "secretA")
    gateway.create_user("userB", "secretB")
    gateway.create_bucket(BUCKET)

    db_path = str(tmp_path / "students.db")
    table(db_path, "students").insert({"name": "phase1"})

    creds_path = tmp_path / "credentials.json"
    write_credentials(creds_path, "userA", "secretA")

    replica_url = gateway.replica_url(BUCKET, "students")
    datasette = Datasette(
        [db_path],
        config={
            "plugins": {
                "datasette-litestream": {
                    "credentials-file": str(creds_path),
                    "credentials-refresh-interval": 0.5,
                }
            },
            "databases": {
                "students": {
                    "plugins": {"datasette-litestream": {"replica": replica_url}}
                }
            },
        },
    )
    await datasette.invoke_startup()

    proc = get_process(datasette)
    assert proc is not None
    assert proc.client is not None
    registered_path = next(iter(proc.registered))

    # Phase 1: userA's credentials replicate successfully. A blocking sync
    # completes remote replication, so local and remote positions agree.
    r1 = proc.client.sync(registered_path, wait=True, timeout=10)
    assert r1["status"] in ("synced", "no_change")
    assert r1["replicated_txid"] == r1["txid"]

    # Phase 2: delete userA server-side. Local WAL-to-LTX syncing still
    # works — only remote replication is broken — so a fire-and-forget sync
    # advances the local position while the remote position stays behind.
    # ("no_change" if the daemon's own 1s sync monitor beat us to the local
    # sync; the position assertions are what matter.)
    gateway.delete_user("userA")
    table(db_path, "students").insert({"name": "phase2"})
    r2 = proc.client.sync(registered_path, wait=False)
    assert r2["status"] in ("synced_local", "no_change")
    assert r2["txid"] > r1["txid"]
    assert r2["replicated_txid"] < r2["txid"]

    # A blocking sync surfaces the upload failure, and specifically an auth
    # failure — not some other breakage.
    with pytest.raises(LitestreamControlError) as excinfo:
        proc.client.sync(registered_path, wait=True, timeout=3)
    assert "InvalidAccessKeyId" in str(excinfo.value)

    # Phase 3: rotate the credentials file to userB and wait for the refresh
    # loop to notice.
    old_hash = proc.current_credentials_hash
    write_credentials(creds_path, "userB", "secretB")
    deadline = time.monotonic() + 15
    while proc.current_credentials_hash == old_hash:
        assert time.monotonic() < deadline, (
            "refresh loop never picked up the rotated credentials"
        )
        await asyncio.sleep(0.1)

    # The hash flips at the start of the restart, so retry until the new
    # daemon is up, re-registered, and syncing with userB's credentials.
    # Recovery takes 10+ seconds: the old daemon retries its final shutdown
    # sync with the dead credentials before it gets killed.
    table(db_path, "students").insert({"name": "phase3"})
    deadline = time.monotonic() + 30
    while True:
        try:
            if proc.client is not None:
                r3 = proc.client.sync(registered_path, wait=True, timeout=10)
                break
        except (LitestreamControlError, httpx.HTTPError):
            pass
        assert time.monotonic() < deadline, (
            "replication did not recover with userB's credentials"
        )
        await asyncio.sleep(0.25)

    # The recovered daemon replicated everything: remote agrees with local
    # and has advanced past everything written during the outage.
    assert r3["status"] in ("synced", "no_change")
    assert r3["replicated_txid"] == r3["txid"]
    assert r3["replicated_txid"] > r2["txid"]

    # Gold standard: restore the replica with userB's credentials and check
    # that every phase's rows made it — including phase2, written while
    # credentials were dead.
    restored = tmp_path / "restored.db"
    await asyncio.to_thread(
        subprocess.run,
        [
            litestream_binary_path(),
            "restore",
            "-o",
            str(restored),
            replica_url,
        ],
        env=dict(
            os.environ, AWS_ACCESS_KEY_ID="userB", AWS_SECRET_ACCESS_KEY="secretB"
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    names = {
        row["name"] for row in sqlite_utils.Database(str(restored))["students"].rows
    }
    assert names == {"phase1", "phase2", "phase3"}
