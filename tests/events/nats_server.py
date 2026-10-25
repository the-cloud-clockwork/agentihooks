import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.events import nats

REQUIRED_ENV = "NATS_SERVER_REQUIRED"


def binary() -> str:
    found = shutil.which("nats-server")
    if found is None:
        if os.environ.get(REQUIRED_ENV) == "1":
            pytest.fail("nats-server is required on PATH")
        pytest.skip("nats-server is not on PATH")
    return found


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_listening(port: int, process: subprocess.Popen, deadline_s: float = 10) -> None:
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


def _wait_url(ports: Path, process: subprocess.Popen, deadline_s: float = 10) -> str:
    stop = time.monotonic() + deadline_s
    while time.monotonic() < stop:
        if process.poll() is not None:
            raise RuntimeError(f"nats-server exited with {process.returncode}")
        try:
            return json.loads(ports.read_text())["nats"][0]
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise RuntimeError("nats-server did not publish its listener")


def durables(bus) -> list[str]:
    return sorted(info.name for info in bus._loop.run(bus._js.consumers_info(nats.EVENT_STREAM)))


@contextmanager
def running(store: Path) -> Iterator[str]:
    executable = binary()
    ports = store.parent / f"{store.name}-ports"
    ports.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [executable, "-js", "-a", "127.0.0.1", "-p", "-1", "-sd", str(store), "--ports_file_dir", str(ports)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield _wait_url(ports / f"{Path(executable).name}_{process.pid}.ports", process)
    finally:
        process.terminate()
        process.wait(10)
