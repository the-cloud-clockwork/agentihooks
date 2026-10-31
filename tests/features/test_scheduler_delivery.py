import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.events.redis_state import RedisStateStore
from scripts.scheduler.core import Scheduler
from scripts.swarm import scheduled_tick
from tests.features.conftest import Swarm


@pytest.mark.feature("scheduler-tick-delivery")
def test_scheduler_delivers_once_under_the_machine_lock_and_records_completion(
    swarm: Swarm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = Scheduler(RedisStateStore(swarm.store.redis, "scheduler"), machine=swarm.ledger.slug)
    for name, value in swarm.ledger.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm.ledger.slug)
    monkeypatch.setattr(scheduled_tick, "Scheduler", lambda: scheduler)
    calls: list[list[str]] = []
    key = swarm.store.key(swarm.ledger.slug, "feature-scheduled-deliveries")
    child = """
import os
from scripts.swarm import scheduled_tick
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import connect

with scheduled_tick.tick_lock():
    store = connect()
    slug = os.environ["AGENTIHOOKS_SWARM"]
    assert LedgerClient().state(slug)["tasks"]
    store.redis.incr(store.key(slug, "feature-scheduled-deliveries"))
"""

    def forbidden(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        pytest.fail(f"a second tick started while the machine lock was held: {argv}")

    def launch(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert argv[-2:] == ["swarm", "tick"]
        calls.append(argv)
        assert scheduled_tick.main(forbidden) == 0
        assert scheduler.read(scheduled_tick.DELIVERED) == ""
        kwargs["env"] = {**os.environ, **kwargs["env"]}
        return subprocess.Popen([sys.executable, "-c", child], **kwargs)

    before = time.time()
    assert scheduled_tick.main(launch) == 0
    assert len(calls) == 1
    assert int(swarm.store.redis.get(key)) == 1
    delivered = json.loads(scheduler.read(scheduled_tick.DELIVERED))
    assert delivered["root"] == str(scheduled_tick.ROOT)
    assert before <= delivered["at"] <= time.time()
