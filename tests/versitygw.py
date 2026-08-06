"""Run a local versitygw S3 gateway for integration tests.

versitygw (https://github.com/versity/versitygw) is a single-binary S3
gateway. We run it with the posix backend rooted in a temp directory and the
internal IAM service (``--iam-dir``), which lets tests create and delete
users at runtime through the admin API — real server-side credential
enforcement without any cloud account.
"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx

ROOT_ACCESS_KEY = "vgw-root"
ROOT_SECRET_KEY = "vgw-root-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VersityGateway:
    """Start/stop a versitygw process and drive its admin API."""

    def __init__(self, base_dir, binary=None):
        binary = binary or shutil.which("versitygw")
        if binary is None:
            raise RuntimeError("versitygw binary not found on PATH")
        self.binary = binary
        self.base_dir = Path(base_dir)
        self.gwroot = self.base_dir / "gwroot"
        self.iam_dir = self.base_dir / "iam"
        self.logfile = self.base_dir / "versitygw.log"
        self.port = _free_port()
        self.admin_port = _free_port()
        self.process = None
        self._log = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def replica_url(self, bucket: str, prefix: str) -> str:
        # litestream parses s3://<bucket>.<host>:<port>/<prefix> into a
        # plain-http endpoint at <host>:<port> with path-style requests.
        return f"s3://{bucket}.127.0.0.1:{self.port}/{prefix}"

    def start(self, timeout=10.0):
        self.gwroot.mkdir(parents=True, exist_ok=True)
        self.iam_dir.mkdir(parents=True, exist_ok=True)
        # Stays open for the daemon's lifetime; closed in stop().
        self._log = open(self.logfile, "wb")  # noqa: SIM115
        self.process = subprocess.Popen(
            [
                self.binary,
                "--port",
                f"127.0.0.1:{self.port}",
                "--admin-port",
                f"127.0.0.1:{self.admin_port}",
                "--iam-dir",
                str(self.iam_dir),
                # Without this, deleted users keep authenticating for up to
                # the IAM cache TTL (120s by default).
                "--iam-cache-disable",
                "--health",
                "/health",
                "posix",
                str(self.gwroot),
            ],
            env=dict(
                os.environ,
                ROOT_ACCESS_KEY=ROOT_ACCESS_KEY,
                ROOT_SECRET_KEY=ROOT_SECRET_KEY,
            ),
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"versitygw exited during startup; see {self.logfile}"
                )
            try:
                response = httpx.get(self.endpoint + "/health", timeout=1.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise RuntimeError(
            f"versitygw not healthy after {timeout}s; see {self.logfile}"
        )

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        if self._log is not None:
            self._log.close()
            self._log = None

    # --- Admin API --------------------------------------------------------

    def admin(self, *args: str):
        return subprocess.run(
            [self.binary, "admin", *args],
            env=dict(
                os.environ,
                ADMIN_ACCESS_KEY_ID=ROOT_ACCESS_KEY,
                ADMIN_SECRET_ACCESS_KEY=ROOT_SECRET_KEY,
                ADMIN_ENDPOINT_URL=f"http://127.0.0.1:{self.admin_port}",
            ),
            check=True,
            capture_output=True,
            text=True,
        )

    def create_user(self, access_key: str, secret_key: str, role="admin"):
        self.admin("create-user", "-a", access_key, "-s", secret_key, "-r", role)

    def delete_user(self, access_key: str):
        self.admin("delete-user", "-a", access_key)

    def create_bucket(self, name: str, owner=ROOT_ACCESS_KEY):
        self.admin("create-bucket", "--bucket", name, "--owner", owner)
