import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REQUIRED_ENV = "NATS_SERVER_REQUIRED"


def binary() -> str:
    found = shutil.which("nats-server")
    if found is None:
        if os.environ.get(REQUIRED_ENV) == "1":
            pytest.fail("nats-server is required on PATH")
        pytest.skip("nats-server is not on PATH")
    return found


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_listening(port: int, process: subprocess.Popen, deadline_s: float = 10) -> None:
    stop = time.monotonic() + deadline_s
    while time.monotonic() < stop:
        if process.poll() is not None:
            raise RuntimeError(f"nats-server exited with {process.returncode}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError(f"nats-server did not listen on {port}")


@contextmanager
def running(store: Path) -> Iterator[str]:
    port = _free_port()
    process = subprocess.Popen(
        [binary(), "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(store)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(port, process)
        yield f"nats://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(10)
