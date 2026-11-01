import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from redis import Redis

from scripts.events.redis_state import RedisStateStore
from scripts.scheduler.core import Scheduler
from scripts.swarm import scheduled_tick
from scripts.swarm.store import RedisStore
from tests.features.conftest import Swarm


@pytest.mark.feature("scheduler-tick-delivery")
def test_scheduler_delivers_once_under_the_machine_lock_and_records_completion(
    swarm: Swarm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = Scheduler(RedisStateStore(swarm.store.redis, "scheduler"), machine=swarm.ledger.slug)
    redis_url = urlsplit(os.environ["AGENTIHOOKS_SWARM_REDIS_URL"])._replace(path="/1").geturl()
    store = RedisStore(Redis.from_url(redis_url, decode_responses=True))
    slug = swarm.ledger.slug
    store.create(replace(swarm.store.config(slug), state="paused", repo=str(tmp_path), snapshot_minutes=0))
    settings = {
        **{
            name: swarm.ledger.env[name]
            for name in ("LEDGER_DIR", "LEDGER_PORT", "LEDGER_AUTOSTART", "AGENTIHOOKS_DEPLOYMENT")
        },
        "AGENTIHOOKS_SWARM_REDIS_URL": redis_url,
        "AGENTIHOOKS_SWARM_DATABASE_URL": os.environ["AGENTIHOOKS_SWARM_DATABASE_URL"],
        "AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES": "0",
        "AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES": "0",
    }
    home = tmp_path / ".agentihooks"
    home.mkdir()
    (home / "feature.env").write_text("".join(f"{name}={value}\n" for name, value in settings.items()))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FEATURE_PARENT_ONLY", "excluded")
    monkeypatch.setattr(scheduled_tick, "Scheduler", lambda: scheduler)
    monkeypatch.setattr(scheduled_tick, "command", lambda binary: [binary, "swarm", "tick"])
    calls: list[list[str]] = []

    def forbidden(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        pytest.fail(f"a second tick started while the machine lock was held: {argv}")

    def launch(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert argv[-2:] == ["swarm", "tick"]
        assert "FEATURE_PARENT_ONLY" not in kwargs["env"]
        assert "AGENTIHOOKS_SWARM_REDIS_URL" not in kwargs["env"]
        calls.append(argv)
        assert scheduled_tick.main(forbidden) == 0
        assert scheduler.read(scheduled_tick.DELIVERED) == ""
        return subprocess.Popen(argv, text=False, **kwargs)

    before = time.time()
    assert scheduled_tick.main(launch) == 0
    assert len(calls) == 1
    completed = store.redis.get(store.key(slug, "last-tick"))
    assert completed is not None
    assert int(completed) >= int(before * 1000)
    delivered = json.loads(scheduler.read(scheduled_tick.DELIVERED))
    assert delivered["root"] == str(scheduled_tick.ROOT)
    assert before <= delivered["at"] <= time.time()
