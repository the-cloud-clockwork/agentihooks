import logging
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.events import nats
from scripts.events.config import BusConfig

from . import nats_server

pytestmark = pytest.mark.unit


@contextmanager
def server(store: Path, port: int) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [nats_server.binary(), "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(store)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        nats_server.wait_listening(port, process)
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(10)


def wait_log(caplog: pytest.LogCaptureFixture, message: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if message in caplog.messages:
            return
        time.sleep(0.01)
    pytest.fail(f"Missing bus lifecycle log: {message}; observed {caplog.messages!r}")


def test_shared_bus_reconnects_and_restores_subscriptions_after_server_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scripts.events.nats")
    port = nats_server.free_port()
    store = tmp_path / "jetstream"
    with server(store, port):
        opened = nats.open_bus(BusConfig(backend="nats", url=f"nats://127.0.0.1:{port}"))
        opened.serve("reconnect.echo", "worker", lambda subject, data: data)
        assert opened.request("reconnect.echo", b"before", 2_000) == b"before"
    try:
        wait_log(caplog, "NATS bus disconnected")
        with server(store, port):
            wait_log(caplog, "NATS bus reconnected")
            assert opened.request("reconnect.echo", b"after", 2_000) == b"after"
        assert caplog.messages.index("NATS bus disconnected") < caplog.messages.index("NATS bus reconnected")
    finally:
        opened.close()
