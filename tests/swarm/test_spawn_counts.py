import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.hub import claims
from scripts.swarm import seat_spawn, status
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
