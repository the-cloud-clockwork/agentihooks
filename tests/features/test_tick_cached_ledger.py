"""A tick pass against a real ledger server behind a proxy that is made slow or stopped runs inside its pass deadline,
and neither that pass nor the next one that reaches the server launches or claims the ready task."""

import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest

from scripts.hive import headroom
from scripts.swarm import cli, tick_watch
from scripts.swarm.ledger_client import LedgerClient, _ledger
from tests.features.conftest import TASK, Swarm, free_port
from tests.swarm.test_tick import FakeRuntime

SLOW_S = 45.0


class Proxy:
    """A TCP proxy to the ledger server that can delay every connection or stop listening."""

    def __init__(self, upstream: int) -> None:
        """Listen on a spare port and forward each connection to ``upstream``."""
        self.upstream, self.port, self.delay_s = upstream, free_port(), 0.0
        self.listener: socket.socket | None = None
        self.start()

    @property
    def base(self) -> str:
        """The address clients send ledger requests to."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        """Listen again on the proxy port."""
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(64)
        self.listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def stop(self) -> None:
        """Shut the listener down and close it so connections are refused."""
        if self.listener is not None:
            self.listener.shutdown(socket.SHUT_RDWR)
            self.listener.close()
            self.listener = None

    def fault(self, kind: str) -> None:
        """Make the ledger slow or stopped."""
        if kind == "slow":
            self.delay_s = SLOW_S
        else:
            self.stop()

    def heal(self) -> None:
        """Answer at full speed again."""
        self.delay_s = 0.0
        if self.listener is None:
            self.start()

    def _accept(self, listener: socket.socket) -> None:
        """Hand every accepted connection to its own forwarding thread."""
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=self._forward, args=(client,), daemon=True).start()

    def _forward(self, client: socket.socket) -> None:
        """Wait the delay, then pipe bytes both ways between the client and the ledger server."""
        time.sleep(self.delay_s)
        try:
            server = socket.create_connection(("127.0.0.1", self.upstream))
        except OSError:
            client.close()
            return
        host = (f"Host: 127.0.0.1:{self.port}".encode(), f"Host: 127.0.0.1:{self.upstream}".encode())
        for source, target, swap in ((client, server, host), (server, client, (b"", b""))):
            threading.Thread(target=self._pipe, args=(source, target, swap), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, target: socket.socket, swap: tuple[bytes, bytes]) -> None:
        """Copy bytes from source to target until either side closes, naming the ledger server's own address in the
        Host header so the server's host check passes."""
        with contextlib.suppress(OSError):
            while chunk := source.recv(65536):
                target.sendall(chunk.replace(*swap) if swap[0] else chunk)
        for side in (source, target):
            with contextlib.suppress(OSError):
                side.shutdown(socket.SHUT_RDWR)


def _roomy(
    pending: int, now_ms: int, proc: Path | None = None, previous: dict[str, object] | None = None
) -> dict[str, float]:
    """A fixed roomy machine reading, so the shared runner's own load never refuses the launch under test."""
    return {
        "cpus": 1000,
        "load5": 0.0,
        "available_mb": 10**7,
        "total_mb": 10**7,
        "pending": pending,
        "measured_at": now_ms,
    }


@pytest.mark.wall_clock
@pytest.mark.feature("swarm-tick-cached-ledger")
@pytest.mark.parametrize("fault", ["slow", "stopped"])
def test_a_tick_keeps_its_deadline_while_the_ledger_is_slow_or_stopped_and_never_launches(
    fault: str, swarm: Swarm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in swarm.ledger.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    monkeypatch.setattr(headroom, "measure", _roomy)
    store, slug = swarm.store, swarm.ledger.slug
    store.update(slug, state="paused", repo=str(tmp_path), snapshot_minutes=0)
    proxy = Proxy(int(swarm.ledger.env["LEDGER_PORT"]))
    monkeypatch.setattr(_ledger(), "BASE", proxy.base)
    ledger, runtime = LedgerClient(), FakeRuntime()
    warmed = cli.run_tick(store, slug, ledger=ledger, runtime=runtime)
    assert "Ledger health probe failed" not in warmed, warmed
    store.update(slug, state="running")
    proxy.fault(fault)
    started = time.monotonic()
    actions = cli.run_tick(store, slug, ledger=ledger, runtime=runtime)
    took = time.monotonic() - started
    assert took < tick_watch.PASS_S, (took, actions)
    assert runtime.spawned == [], actions
    assert store.claimant(slug, TASK) is None
    proxy.heal()
    actions = cli.run_tick(store, slug, ledger=ledger, runtime=runtime)
    assert "Ledger health probe failed" not in actions, actions
    assert runtime.spawned == [], actions
    task = ledger.task(slug, TASK)
    assert task is not None
    assert (store.claimant(slug, TASK), task["claimed_by"]) == (None, "")
