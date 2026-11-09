"""A hive's agent ceiling: its own reported memory, swap and load under the host room rule, bounded by a set cap."""

import json
from typing import Any, cast

import pytest

from scripts.hive import ceiling, headroom, launch, registry
from scripts.swarm import host_budget
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.unit

GB = 1024


def reading(**fields: object) -> dict:
    return {
        "cpus": 8,
        "load5": 1.0,
        "load1": 1.0,
        "available_mb": 16 * GB,
        "total_mb": 32 * GB,
        "swap_used_mb": 0,
        "swap_total_mb": 0,
        "agents": {"claude": 2, "codex": 1},
        "pending": 0,
        "measured_at": 1,
        **fields,
    }


@pytest.mark.feature("hive-agent-ceiling")
def test_an_uncapped_ceiling_moves_with_the_hives_free_memory() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    assert ceiling.ceiling(None, reading(available_mb=7 * per)) == 3 + 7
    assert ceiling.ceiling(None, reading(available_mb=20 * per)) == 3 + 20
    assert ceiling.ceiling(None, reading(available_mb=20 * per + per - 1)) == 3 + 20


@pytest.mark.feature("hive-agent-ceiling")
def test_swap_in_use_counts_against_the_free_memory() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    found = reading(available_mb=10 * per, swap_used_mb=4 * per, swap_total_mb=40 * per)
    assert ceiling.ceiling(None, found) == 3 + 6


@pytest.mark.feature("hive-agent-ceiling")
def test_a_load_above_the_high_watermark_holds_the_ceiling_at_the_agents_running() -> None:
    assert ceiling.ceiling(None, reading(load1=8 * host_budget.LOAD_HIGH + 0.1)) == 3


@pytest.mark.feature("hive-agent-ceiling")
def test_the_one_minute_load_falls_back_to_the_five_minute_load_of_an_older_daemon() -> None:
    older = reading(load5=8 * host_budget.LOAD_HIGH + 0.1)
    del older["load1"]
    assert ceiling.ceiling(None, older) == 3


@pytest.mark.feature("hive-agent-ceiling")
def test_the_pod_memory_limit_binds_when_it_is_lower_than_the_node() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    assert ceiling.ceiling(None, reading(available_mb=30 * per, pod_available_mb=5 * per)) == 3 + 5
    assert ceiling.ceiling(None, reading(available_mb=4 * per, pod_available_mb=5 * per)) == 3 + 4


@pytest.mark.feature("hive-agent-ceiling")
def test_a_set_cap_holds_the_ceiling_lower() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    assert ceiling.ceiling(5, reading(available_mb=20 * per)) == 5
    assert ceiling.ceiling(50, reading(available_mb=20 * per)) == 23


@pytest.mark.feature("hive-agent-ceiling")
def test_without_a_reading_the_ceiling_is_the_cap_or_none() -> None:
    assert ceiling.ceiling(4, None) == 4
    assert ceiling.ceiling(None, None) == 0
    assert ceiling.computed(None) is None
    assert ceiling.computed({"cpus": 8}) is None


@pytest.mark.feature("hive-agent-ceiling")
def test_a_reading_without_agents_counts_none_running() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    found = reading(available_mb=6 * per)
    del found["agents"]
    assert ceiling.computed(found) == 6


@pytest.fixture
def redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.mark.feature("hive-agent-ceiling")
def test_a_new_hive_carries_no_cap_and_auto_clears_a_set_one(redis: Any) -> None:
    assert registry.update(redis, "box", ["ui=no"])["max_agents"] is None
    assert registry.update(redis, "head", ["ui=yes"])["max_agents"] is None
    assert registry.update(redis, "box", ["max-agents=6"])["max_agents"] == 6
    assert registry.update(redis, "box", ["max-agents=auto"])["max_agents"] is None
    shown = registry.show(redis, "box")
    assert shown is not None
    assert shown["max_agents"] is None


@pytest.mark.feature("hive-agent-ceiling")
def test_placement_reads_the_computed_ceiling_and_keeps_the_cap(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    per = host_budget.MEMORY_PER_AGENT_MB
    registry.update(redis, "free", ["ui=no"])
    registry.update(redis, "capped", ["ui=no", "max-agents=4"])
    for hive in ("free", "capped"):
        redis.set(headroom.key(hive), json.dumps(reading(available_mb=12 * per, measured_at=10)))
    store = cast("RedisStore", SimpleNamespace(redis=redis, slugs=lambda: [], agents=lambda slug: []))
    monkeypatch.setattr(launch.lease, "now_ms", lambda store: 20)
    found = {hive["id"]: hive for hive in launch.loaded_hives(store)}
    assert (found["free"]["max_agents"], found["free"]["cap"]) == (15, None)
    assert (found["capped"]["max_agents"], found["capped"]["cap"]) == (4, 4)
    assert (found["free"]["unread"], found["capped"]["unread"]) == (False, False)
    registry.update(redis, "dark", ["ui=no"])
    dark = {hive["id"]: hive for hive in launch.loaded_hives(store)}["dark"]
    assert (dark["max_agents"], dark["unread"]) == (0, True)
    redis.set(headroom.key("free"), json.dumps(reading(available_mb=2 * per, measured_at=10)))
    assert {hive["id"]: hive for hive in launch.loaded_hives(store)}["free"]["max_agents"] == 5


@pytest.mark.feature("hive-agent-ceiling")
def test_a_reading_without_swap_counts_none_and_a_reported_swap_counts() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    older = reading(available_mb=5 * per)
    del older["swap_used_mb"], older["swap_total_mb"]
    assert ceiling.ceiling(None, older) == 3 + 5
    assert ceiling.ceiling(None, reading(available_mb=5 * per, swap_used_mb=per, swap_total_mb=40 * per)) == 3 + 4


@pytest.mark.feature("hive-agent-ceiling")
def test_shown_is_none_only_without_a_cap_and_a_reading() -> None:
    per = host_budget.MEMORY_PER_AGENT_MB
    assert ceiling.shown(None, None) is None
    assert ceiling.shown(4, None) == 4
    assert ceiling.shown(None, reading(available_mb=6 * per)) == 9
    assert ceiling.shown(4, reading(available_mb=6 * per)) == 4


@pytest.mark.feature("hive-agent-ceiling")
def test_auto_removes_the_stored_cap(redis: Any) -> None:
    registry.update(redis, "box", ["max-agents=6"])
    registry.update(redis, "box", ["max-agents=auto"])
    assert "max_agents" not in (registry._stored(redis, "box") or {})  # pyright: ignore[reportPrivateUsage]


@pytest.mark.feature("hive-agent-ceiling")
def test_the_hive_list_shows_the_ceiling_and_how_it_is_set(
    redis: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import cli

    per = host_budget.MEMORY_PER_AGENT_MB
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(cli, "now_ms", lambda: 20)
    for hive_id, pairs in (("a", ["ui=no"]), ("b", ["ui=no", "max-agents=3"]), ("c", ["ui=no", "max-agents=30"])):
        registry.update(redis, hive_id, pairs)
        redis.set(headroom.key(hive_id), json.dumps(reading(available_mb=7 * per, measured_at=10)))
    registry.update(redis, "d", ["ui=no"])
    assert cli.main(["list"]) == 0
    columns = {line.split("\t")[0]: line.split("\t")[3] for line in capsys.readouterr().out.splitlines()}
    assert columns == {
        "a": "workers=0 of 10 (auto)",
        "b": "workers=0 of 3 (cap 3)",
        "c": "workers=0 of 10 (cap 30)",
        "d": "workers=0 of unknown (auto)",
    }


@pytest.mark.feature("hive-agent-ceiling")
def test_the_root_probe_ceiling_follows_the_cap_the_reading_or_the_local_limit(redis: Any) -> None:
    from scripts.swarm import probe

    per = host_budget.MEMORY_PER_AGENT_MB
    registry.update(redis, probe.ROOT, ["ui=yes"])
    assert probe.root_ceiling(redis, 20, 15) == 15
    redis.set(headroom.key(probe.ROOT), json.dumps(reading(available_mb=6 * per, measured_at=10)))
    assert probe.root_ceiling(redis, 20, 15) == 9
    registry.update(redis, probe.ROOT, ["max-agents=4"])
    assert probe.root_ceiling(redis, 20, 15) == 4
    redis.delete(headroom.key(probe.ROOT))
    assert probe.root_ceiling(redis, 20, 15) == 4
