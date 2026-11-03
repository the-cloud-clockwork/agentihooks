import json
import os
import subprocess
import sys
import time

import fakeredis
import pytest

from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_master_recovery import GRACE_MS, NOW, RecoveryRuntime, _hive, _hive_master
from tests.swarm.test_tick import tasks


def test_cold_fail_over_imports() -> None:
    if not os.environ.get("PROBE_CHILD"):
        pytest.skip("parent")
    from scripts.hive import registry
    from scripts.swarm import master_move
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
    seen: dict = {}
    original = master_move.fail_over

    def fail(*args: object) -> list:
        before = set(sys.modules)
        started = time.perf_counter()
        found = original(*args)
        seen["seconds"] = round(time.perf_counter() - started, 3)
        seen["imported"] = sorted(name for name in set(sys.modules) - before if name.startswith(("scripts", "hooks")))
        seen["foreign"] = len([name for name in set(sys.modules) - before if not name.startswith(("scripts", "hooks"))])
        return found

    master_move.fail_over = fail
    try:
        before = set(sys.modules)
        tick("sw", store, tasks(), RecoveryRuntime(), NOW)
        seen["tick_imported"] = sorted(n for n in set(sys.modules) - before if n.startswith(("scripts", "hooks")))
    finally:
        master_move.fail_over = original
    print("PROBE " + json.dumps(seen))


@pytest.mark.parametrize("attempt", range(3))
def test_cold_fail_over_probe(attempt: int) -> None:
    if os.environ.get("PROBE_CHILD"):
        pytest.skip("child")
    node = "tests/swarm/test_master_move_cold_probe.py::test_cold_fail_over_imports"
    command = [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", node]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PROBE_CHILD": "1"})
    line = next((row for row in done.stdout.splitlines() if "PROBE " in row), done.stdout[-2000:])
    raise AssertionError(line)
