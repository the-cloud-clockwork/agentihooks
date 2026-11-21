import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.redis_state import RedisStateStore
from scripts.scheduler import process_timer, service
from scripts.scheduler.core import Scheduler
from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.events.publishing import Publishing
from tests.swarm_ledger.test_sqlite import CONTENT, store

CHECKOUT = Path(__file__).resolve().parents[2]
MARGIN_S = 0.25
WRITES = 4
SPACING_S = 0.3
SCHEDULER_PASSES = 60
DELIVERY_WAIT_S = 5.0
WRITER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from scripts.swarm_ledger.repository import sqlite\n"
    "repository = sqlite.SQLiteLedgerRepository(Path(sys.argv[1]) / 'ledgers' / sqlite.DATABASE)\n"
    "for n in range(int(sys.argv[2])):\n"
    "    time.sleep(float(sys.argv[3]))\n"
    "    repository.apply_ops('ledger', ops=[{'op': 'add', 'id': f'm{n}', 'thread': 'chat', 'text': f'message {n}'}])\n"
    "    print(time.monotonic(), flush=True)\n"
)


@pytest.mark.feature("ledger-server-follow-wakes-on-its-own-interval")
def test_the_shared_follow_waits_at_most_its_own_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[tuple[float, float | None]] = []

    def wait(delay: float, state: object = None, timeout: float | None = None) -> None:
        waits.append((delay, timeout))
        raise StopIteration

    monkeypatch.setattr(server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(server.HUB, "watched", lambda: [])
    monkeypatch.setattr(process_timer, "wait", wait)
    with pytest.raises(StopIteration):
        server.follow_shared()
    assert waits == [(server.FOLLOW_S, server.FOLLOW_S)]


@pytest.mark.wall_clock
@pytest.mark.feature("ledger-server-follow-wakes-on-its-own-interval")
def test_a_write_another_process_stores_reaches_the_publisher_within_the_follow_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fakeredis

    state = RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler")
    monkeypatch.setattr(process_timer, "Scheduler", lambda _state=None: Scheduler(state))
    monkeypatch.setattr(service, "ensure_running", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "SCHEDULER_UNAVAILABLE", threading.Event())
    arrived: dict[str, float] = {}

    def publish(slug: str, document: dict[str, Any]) -> None:
        now = time.monotonic()
        for message in document.get("chat", []):
            arrived.setdefault(message["id"], now)

    store(tmp_path).create("ledger", CONTENT)
    reader = Publishing(store(tmp_path), publish)
    reader.current("ledger")
    reader.drain(5)
    stopping, finished = threading.Event(), threading.Event()
    scheduled_wait = server.scheduled_wait

    def paced(interval: float, timeout: float | None = None) -> None:
        if stopping.is_set():
            raise SystemExit
        scheduled_wait(interval, timeout=timeout)

    def scheduler_passes() -> None:
        for _ in range(SCHEDULER_PASSES):
            if finished.wait(service.PASS_S):
                return
            Scheduler(state).run_due()

    monkeypatch.setattr(server, "repository", reader)
    monkeypatch.setattr(server, "HUB", SimpleNamespace(watched=lambda: ["ledger"]))
    monkeypatch.setattr(server, "scheduled_wait", paced)
    follow = threading.Thread(target=server.follow_shared, daemon=True)
    passes = threading.Thread(target=scheduler_passes, daemon=True)
    passes.start()
    follow.start()
    try:
        written = subprocess.run(
            [sys.executable, "-c", WRITER, str(tmp_path), str(WRITES), str(SPACING_S)],
            cwd=CHECKOUT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        deadline = time.monotonic() + DELIVERY_WAIT_S
        while len(arrived) < WRITES and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stopping.set()
        follow.join(timeout=SCHEDULER_PASSES * service.PASS_S)
        finished.set()
        passes.join(timeout=service.PASS_S * 2)
        reader.drain(5)
    stored = [float(line) for line in written.stdout.split()]
    delays = [arrived.get(f"m{n}", float("inf")) - at for n, at in enumerate(stored)]
    assert len(stored) == WRITES
    assert max(delays) < server.FOLLOW_S + MARGIN_S, delays
