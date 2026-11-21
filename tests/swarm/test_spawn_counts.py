"""Swarm status counts the spawns placed per harness over the last hour."""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.hub import claims
from scripts.swarm import status
from scripts.swarm import store as store_module
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig
from tests.hub.test_claim_lives import started

HOUR = 3_600_000


def now_ms() -> int:
    return int(time.time() * 1000)


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_status_counts_one_claude_and_one_codex_spawn_placed_in_the_last_hour(rig: SimpleNamespace) -> None:
    now = now_ms()
    event = started(rig.admitter.admit(SLUG, ask()))
    claims.Claims(rig.store).handle(replace(event, data={**event.data, "harness": "codex", "launched_at": now}))
    rig.store.put_agent(SLUG, AgentRecord("master-a", "master", "", harness="claude", launched_at=now - 60_000))
    rig.store.put_agent(SLUG, AgentRecord("master-b", "master", "", harness="claude", launched_at=now - 2 * HOUR))

    assert status.status_report(rig.store, SLUG, {"tasks": []})["spawns"] == {"claude": 1, "codex": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_redelivered_start_and_a_rewritten_placed_agent_count_one_spawn_each(rig: SimpleNamespace) -> None:
    now = now_ms()
    event = started(rig.admitter.admit(SLUG, ask()))
    event = replace(event, data={**event.data, "harness": "codex", "launched_at": now})
    unplaced = AgentRecord("master-a", "master", "", harness="claude", state="starting")
    claims.Claims(rig.store).handle(event)
    claims.Claims(rig.store).handle(replace(event, id="redelivery"))
    rig.store.put_agent(SLUG, unplaced)
    rig.store.put_agent(SLUG, replace(unplaced, launched_at=now))
    rig.store.put_agent(SLUG, replace(unplaced, state="working", launched_at=now - 2 * HOUR))
    rig.store.put_agent(SLUG, AgentRecord("eng-d", "eng", "t1", launched_at=now))

    assert rig.store.spawns(SLUG) == {"claude": 1, "codex": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_start_without_a_harness_or_launch_time_counts_as_unknown_at_the_event_time(rig: SimpleNamespace) -> None:
    event = started(rig.admitter.admit(SLUG, ask()))

    claims.Claims(rig.store).handle(replace(event, time=now_ms()))

    assert rig.store.spawns(SLUG) == {"unknown": 1}


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, ".", 0, 0))
    return found


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_the_count_keeps_a_spawn_exactly_one_hour_old_and_drops_an_older_one(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000_000
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    store.record_spawn(SLUG, "claude", "eng-a", now - store_module.SPAWN_WINDOW_MS)
    store.record_spawn(SLUG, "codex", "eng-b", now - store_module.SPAWN_WINDOW_MS - 1)

    assert store_module.SPAWN_WINDOW_MS == HOUR
    assert store.spawns(SLUG) == {"claude": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_the_spawn_record_keeps_only_the_newest_spawns(store: RedisStore) -> None:
    for n in range(store_module.SPAWNS_KEPT + 1):
        store.record_spawn(SLUG, "claude", f"eng-{n}", n)

    kept = store.redis.zrange(store.key(SLUG, "spawn-times"), 0, -1)
    assert len(kept) == store_module.SPAWNS_KEPT == 1000
    assert kept[0] == "claude:eng-1"


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_text_status_prints_the_last_hour_spawns_per_harness(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import cli
    from tests.swarm.test_tick import FakeLedger

    now = now_ms()
    for name, harness in (("eng-a", "codex"), ("eng-b", "claude"), ("eng-c", "claude")):
        store.put_agent(SLUG, AgentRecord(name, "eng", "t1", harness=harness, launched_at=now))
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(cli, "live_agents", lambda agents, runtime: [])
    cli.cmd_status(store, SimpleNamespace(slug=SLUG, json=False))

    assert "spawns last hour  claude 2  codex 1" in capsys.readouterr().out.splitlines()


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_the_spawn_line_sorts_harnesses_and_names_an_empty_hour() -> None:
    assert status.spawns_line({"codex": 1, "claude": 2}) == "spawns last hour  claude 2  codex 1"
    assert status.spawns_line({}) == "spawns last hour  none"
