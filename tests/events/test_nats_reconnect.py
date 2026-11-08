import logging
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope

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
        if ("scripts.events.nats", message) in [(record.name, record.getMessage()) for record in caplog.records]:
            return
        time.sleep(0.01)
    pytest.fail(f"Missing bus lifecycle log: {message}; observed {caplog.messages!r}")


def test_shared_bus_reconnects_and_restores_subscriptions_after_server_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scripts.events.nats")
    port = nats_server.free_port()
    store = tmp_path / "jetstream"
    with server(store, port) as first:
        opened = nats.open_bus(BusConfig(backend="nats", url=f"nats://127.0.0.1:{port}"))
        try:
            opened.serve("reconnect.echo", "worker", lambda subject, data: data)
            assert opened.request("reconnect.echo", b"before", 2_000) == b"before"
            queue = opened.queue("work", QueuePolicy(max_deliver=3, ack_wait_ms=1_000))
            first.terminate()
            first.wait(10)
            wait_log(caplog, "NATS bus disconnected")
            with server(store, port):
                wait_log(caplog, "NATS bus reconnected")
                assert opened.request("reconnect.echo", b"after", 2_000) == b"after"
                queue.offer(
                    Envelope(
                        id="after",
                        type="task.claimed",
                        swarm="s",
                        entity="task",
                        key="t1",
                        version=1,
                        time=0,
                        author="a",
                        data={},
                    )
                )
                delivery = queue.take(2_000)
                assert delivery is not None and delivery.event.id == "after"
            assert caplog.messages.index("NATS bus disconnected") < caplog.messages.index("NATS bus reconnected")
        finally:
            opened.close()


def test_closing_a_shared_bus_logs_no_disconnect(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="scripts.events.nats")
    port = nats_server.free_port()
    with server(tmp_path / "jetstream", port):
        nats.open_bus(BusConfig(backend="nats", url=f"nats://127.0.0.1:{port}")).close()
    assert [record.getMessage() for record in caplog.records if record.name == "scripts.events.nats"] == []
