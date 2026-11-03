import json
import os
import subprocess
import sys

import fakeredis
import pytest

from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_master_recovery import GRACE_MS, NOW, RecoveryRuntime, _hive, _hive_master
from tests.swarm.test_tick import tasks


def _scenario() -> tuple[list, list]:
    from scripts.hive import registry
    from tests.hive.test_controller_launch import publish_room

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    _hive(store, "lost", NOW - GRACE_MS - 1)
    for hive, room, workers in (("big", 0, 8), ("small", 2, 6)):
        _hive(store, hive, NOW)
        registry.beat(store.redis, hive, {"max_agents": 8})
        store.redis.set(f"{registry.key(hive)}:sessions", json.dumps({"claude:one": workers}))
        publish_room(store, hive, room)
    _hive_master(store)
    runtime = RecoveryRuntime()
    actions = tick("sw", store, tasks(), runtime, NOW)
    return actions, runtime.masters


def test_probe_in_process() -> None:
    bad = []
    for attempt in range(300):
        actions, launched = _scenario()
        if [task.get("hive") for _, task in launched] != ["small"]:
            bad.append((attempt, actions, [t.get("hive") for _, t in launched]))
    assert not bad, json.dumps(bad[:5], default=str)


def test_probe_once() -> None:
    actions, launched = _scenario()
    assert [task.get("hive") for _, task in launched] == ["small"], json.dumps(actions, default=str)


@pytest.mark.parametrize("attempt", range(40))
def test_probe_cold_process(attempt: int) -> None:
    if os.environ.get("PROBE_CHILD"):
        pytest.skip("child")
    node = "tests/swarm/test_master_recovery_probe.py::test_probe_once"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "--tb=short", node]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PROBE_CHILD": "1"})
    assert done.returncode == 0, done.stdout[-4000:]
