import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from nats.js.errors import APIError

from scripts.events import nats

REQUIRED_ENV = "NATS_SERVER_REQUIRED"
UNMATCHED_MSG_ID = "never-a-last-msg-id"
WRONG_LAST_MSG_ID = 10070


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


def durables(bus) -> list[str]:
    return sorted(info.name for info in bus._loop.run(bus._js.consumers_info(nats.EVENT_STREAM)))


def remembers(bus, subject: str, stream: str, event_id: str) -> bool:
    headers = {nats.MSG_ID: event_id, "Nats-Expected-Last-Msg-Id": UNMATCHED_MSG_ID}
    try:
        ack = bus._loop.run(bus._js.publish(subject, b"", stream=stream, headers=headers))
    except APIError as error:
        if error.err_code != WRONG_LAST_MSG_ID:
            raise
        return False
    return ack.duplicate


def wait_forgotten(bus, subject: str, stream: str, event_id: str, deadline_s: float = 10) -> None:
    stop = time.monotonic() + deadline_s
    while remembers(bus, subject, stream, event_id):
        if time.monotonic() >= stop:
            pytest.fail(f"{stream} still remembers message id {event_id!r} after {deadline_s}s")
        time.sleep(0.01)


@contextmanager
def running(store: Path) -> Iterator[str]:
    port = free_port()
    process = subprocess.Popen(
        [binary(), "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(store)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_listening(port, process)
        yield f"nats://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(10)
