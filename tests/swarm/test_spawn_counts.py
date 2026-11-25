import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.hub import claims
from scripts.swarm import seat_spawn, spawn_counts, status
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import Placed
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig
from tests.hub.test_claim_lives import started

HOUR = 3_600_000


def now_ms() -> int:
    return int(time.time() * 1000)


def local_spawn(store: RedisStore, harness: str, at: int) -> AgentRecord:
    runtime = SimpleNamespace(spawn=lambda *args: Placed("local:pane", harness, launched_at=at))
    record, _ = seat_spawn.place_seat(
        SLUG, store.config(SLUG), store, runtime, seat_spawn.Seat("master", "master"), at, lambda record: {}
    )
    return record


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_status_counts_one_claude_and_one_codex_spawn_placed_in_the_last_hour(rig: SimpleNamespace) -> None:
    now = now_ms()
    event = started(rig.admitter.admit(SLUG, ask()))
    claims.Claims(rig.store).handle(replace(event, data={**event.data, "harness": "codex", "launched_at": now}))
    local_spawn(rig.store, "claude", now - 60_000)
    local_spawn(rig.store, "claude", now - 2 * HOUR)

    assert status.status_report(rig.store, SLUG, {"tasks": []})["spawns"] == {"claude": 1, "codex": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_redelivered_start_and_a_rewritten_placed_agent_count_one_spawn_each(rig: SimpleNamespace) -> None:
    now = now_ms()
    event = started(rig.admitter.admit(SLUG, ask()))
    event = replace(event, data={**event.data, "harness": "codex", "launched_at": now})
    claims.Claims(rig.store).handle(event)
    claims.Claims(rig.store).handle(replace(event, id="redelivery"))
    placed = local_spawn(rig.store, "claude", now)
    rig.store.put_agent(SLUG, replace(placed, state="working", launched_at=now + 1))
    rig.store.put_agent(SLUG, AgentRecord("master-c", "master", "", harness="claude"))

    assert rig.store.spawns(SLUG) == {"claude": 1, "codex": 1}


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, ".", 0, 0))
    return found


@pytest.mark.parametrize(
    ("counts", "line"),
    [({"codex": 1, "claude": 2}, "spawns last hour  claude 2  codex 1"), ({}, "spawns last hour  none")],
)
@pytest.mark.feature("swarm-status-hourly-spawns")
def test_text_status_prints_the_last_hour_spawns_per_harness(
    store: RedisStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    counts: dict[str, int],
    line: str,
) -> None:
    from scripts.swarm import cli
    from tests.swarm.test_tick import FakeLedger

    monkeypatch.setattr(store, "spawns", lambda slug: counts)
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(cli, "live_agents", lambda agents, runtime: [])
    cli.cmd_status(store, SimpleNamespace(slug=SLUG, json=False))

    assert line in capsys.readouterr().out.splitlines()


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_spawn_window_includes_both_boundaries_ignores_future_and_reads_without_writes(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 10_000_000
    monkeypatch.setattr(spawn_counts.time, "time", lambda: now / 1000)
    store.redis.hset(store.key(SLUG, "spawns"), "claude", 99)
    for name, harness, at in (
        ("old", "claude", now - HOUR - 1),
        ("lower", "claude", now - HOUR),
        ("upper", "codex", now),
        ("future", "codex", now + 1),
    ):
        spawn_counts.record(store, SLUG, harness, name, at)
    assert store.redis.zcard(store.key(SLUG, "spawned")) == 3
    before = store.export(SLUG)
    assert store.spawns(SLUG) == {"claude": 1, "codex": 1}
    assert store.export(SLUG) == before
    monkeypatch.setattr(spawn_counts.time, "time", lambda: (now + HOUR + 2) / 1000)
    assert store.spawns(SLUG) == {}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_spawn_identity_preserves_relaunches_and_deduplicates_replayed_launches(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 10_000_000
    monkeypatch.setattr(spawn_counts.time, "time", lambda: now / 1000)
    for harness, name, at in (
        ("claude", "same", now - 1),
        ("claude", "same", now),
        ("claude", "other", now),
        ("codex", "same", now),
    ):
        spawn_counts.record(store, SLUG, harness, name, at)
        spawn_counts.record(store, SLUG, harness, name, at)
    assert store.spawns(SLUG) == {"claude": 3, "codex": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
@pytest.mark.parametrize(("harness", "at"), [("", 1), ("claude", 0), ("codex", -1)])
def test_an_unplaced_record_adds_no_spawn(
    store: RedisStore, harness: str, at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn_counts.time, "time", lambda: 0.001)
    before = store.export(SLUG)
    spawn_counts.record(store, SLUG, harness, "unplaced", at)
    assert store.export(SLUG) == before


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_start_already_counted_as_a_claim_still_repairs_its_missing_spawn(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = started(rig.admitter.admit(SLUG, ask()))
    event = replace(event, data={**event.data, "harness": "codex", "launched_at": now_ms()})
    with monkeypatch.context() as patch:
        patch.setattr(spawn_counts, "record", lambda *args: None)
        claims.Claims(rig.store).handle(event)
    assert rig.store.spawns(SLUG) == {}
    claims.Claims(rig.store).handle(event)
    assert rig.store.spawns(SLUG) == {"codex": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_same_name_master_resume_adds_a_new_launch(store: RedisStore) -> None:
    from scripts.swarm import master_launch

    now = now_ms()
    original = local_spawn(store, "claude", now - 1000)
    agent = replace(original, harness="claude", profile="engineer", launched_at=now - 1000)
    runtime = SimpleNamespace(resume=lambda *args: Placed("resumed:pane", "claude", launched_at=now))
    master_launch._resume(store, SLUG, runtime, now - HOUR - 1, master_launch.Previous(agent, now - 1000))
    assert store.spawns(SLUG) == {"claude": 2}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_local_launch_refused_during_admission_adds_no_spawn(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from scripts.swarm_v2.runtime import codex

    def refuse(*args: Any) -> None:
        raise RuntimeError("admission refused")

    monkeypatch.setattr(codex, "admit_legacy", refuse)
    with pytest.raises(seat_spawn.SeatFailed, match="admission refused"):
        local_spawn(store, "claude", now_ms())
    assert store.spawns(SLUG) == {}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_resumed_process_stays_counted_when_its_metadata_write_fails(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from scripts.swarm import resume, snapshot

    now = now_ms()
    agent = AgentRecord("eng-a", "eng", "t1", harness="claude", pane_id="old")
    runtime = SimpleNamespace(resume=lambda *args: Placed("new", "claude", launched_at=now))
    context = SimpleNamespace(runtime=runtime, at=now)
    monkeypatch.setattr(snapshot, "worktrees", lambda *args: {})
    monkeypatch.setattr(resume, "blocker", lambda *args: "")
    monkeypatch.setattr(resume, "_recovery_current", lambda *args: True)

    def refused(*args: Any) -> None:
        raise OSError("metadata write refused")

    monkeypatch.setattr(store, "put_agent", refused)
    assert resume._recover_process(store, SLUG, agent, {}, context, "resume") == "metadata write refused"
    assert store.spawns(SLUG) == {"claude": 1}


@pytest.mark.feature("swarm-status-hourly-spawns")
def test_a_local_launch_is_retired_when_its_spawn_cannot_be_recorded(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any
    from unittest.mock import Mock

    def refused(*args: Any) -> None:
        raise OSError("spawn record refused")

    runtime = SimpleNamespace(spawn=lambda *args: Placed("local:pane", "claude", launched_at=now_ms()), retire=Mock())
    monkeypatch.setattr(spawn_counts, "record", refused)
    with pytest.raises(seat_spawn.SeatFailed, match="spawn record refused"):
        seat_spawn.place_seat(
            SLUG, store.config(SLUG), store, runtime, seat_spawn.Seat("master", "master"), now_ms(), lambda record: {}
        )
    retired = runtime.retire.call_args.args[0]
    assert (retired.pane_id, retired.harness) == ("local:pane", "claude")
    runtime.retire.assert_called_once()
