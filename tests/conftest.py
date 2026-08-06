import os
import shutil
import pytest

from datasette_litestream.process import processes


def litestream_binary_path():
    """Path to a litestream binary for integration tests, or None.

    Honors LITESTREAM_TEST_BINARY, then DATASETTE_LITESTREAM_BINARY, then PATH.
    Integration tests need litestream >= 0.5 (control socket support).
    """
    return (
        os.environ.get("LITESTREAM_TEST_BINARY")
        or os.environ.get("DATASETTE_LITESTREAM_BINARY")
        or shutil.which("litestream")
    )


@pytest.fixture(scope="session", autouse=True)
def _configure_binary():
    """Point the plugin at the test binary for the whole session."""
    binary = litestream_binary_path()
    if binary:
        os.environ["DATASETTE_LITESTREAM_BINARY"] = binary
    yield


@pytest.fixture
def litestream_binary():
    """Skip the test if no litestream binary is available."""
    binary = litestream_binary_path()
    if not binary:
        pytest.skip(
            "litestream binary not available; set LITESTREAM_TEST_BINARY to a "
            "litestream >= 0.5 binary to run integration tests"
        )
    return binary


@pytest.fixture(autouse=True)
def _cleanup_daemons():
    """Stop any litestream daemons started during a test and reset global state."""
    yield
    for proc in list(processes.values()):
        task = getattr(proc, "_refresh_task", None)
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass
        try:
            proc.stop_daemon()
        except Exception:
            pass
    processes.clear()
