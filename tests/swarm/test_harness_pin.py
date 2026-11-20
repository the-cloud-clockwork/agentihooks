"""A lane pinned to one harness while the other harness has free quota seats and ready lane tasks wait raises a health
finding and shows in the swarm status quota lines."""

import json
from dataclasses import replace

import pytest

from scripts.swarm import capacity, quota_view
from scripts.swarm.health import harness_pin
from scripts.swarm.store import MANUAL_SCALING, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger

SLUG = "sw"
START = 1_794_000_000_000
DAY = 86_400_000
IDLE = {"eng": 0, "ci": 0, "plan": 0}


def seat(harness: str, name: str, cap: int, sessions: int) -> capacity.Account:
    return capacity.Account(harness, name, capacity._state(cap, None), sessions, 90, 90, cap)


CLAUDE_FULL = seat("claude", "a", 2, 2)
CLAUDE_FREE = seat("claude", "a", 3, 0)
CODEX_FULL = seat("codex", "x", 6, 6)
CODEX_FREE = seat("codex", "x", 6, 0)


class QuotaRuntime:
    def __init__(self, rows: list[capacity.Account]) -> None:
        self.rows = rows

    def quota_capacity(
        self,
        config: SwarmConfig,
        agents: list[AgentRecord],
        now: float,
        demand: dict[str, int] | None,
        requirements: dict[str, list[tuple[str, ...]]] | None,
    ) -> dict:
        return capacity.calculate(config, self.rows, agents, demand)


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def swarm(store: RedisStore, lanes: dict[str, dict[str, str]], max_eng: int = 4) -> SwarmConfig:
    config = SwarmConfig(SLUG, "/repo", max_eng=max_eng, max_ci=0, max_plan=0, lanes=lanes)
    store.create(config)
    return config


def apply(
    store: RedisStore,
    config: SwarmConfig,
    rows: list[capacity.Account],
    now_ms: int,
    tasks: tuple[str, ...] = ("e1", "e2"),
) -> dict:
    ledger = FakeLedger([{"id": task} for task in tasks])
    ledger.comment = lambda *args, **kwargs: None
    capacity.apply(SLUG, config, store, ledger, QuotaRuntime(rows), now_ms)
    return capacity.read(store, SLUG)


@pytest.mark.feature("swarm-harness-pin-guard")
def test_a_claude_pin_with_free_codex_seats_and_waiting_tasks_raises_the_finding(store: RedisStore) -> None:
    """A Claude pin with free Codex seats and two waiting engineer tasks raises the finding, naming all of it."""
    config = swarm(store, {"eng": {"agent": "claude"}})
    apply(store, config, [CLAUDE_FULL, CODEX_FREE], START)
    apply(store, config, [CLAUDE_FULL, CODEX_FREE], START + 6 * DAY)
    [found] = harness_pin.findings(store, SLUG, START + 6 * DAY)
    assert found.id == "harness-pin/eng"
    assert found.summary == (
        "Lane eng is pinned to Claude, first seen 6d00h ago, while Codex has 6 free quota seats and 2 ready tasks wait"
    )
    assert found.evidence == (
        "The eng lane agent is claude, so capacity offers its tasks only Claude seats",
        "Codex free quota seats: 6",
        "Ready eng tasks waiting: e1, e2",
        "Pin first seen by the capacity pass 6d00h ago",
    )
    assert (found.threshold, found.measure) == ("a free seat on the other harness", 2)


@pytest.mark.feature("swarm-harness-pin-guard")
def test_a_codex_pin_with_free_claude_seats_raises_the_reverse_finding(store: RedisStore) -> None:
    """The guard is harness neutral: a Codex pin over free Claude seats is the same finding."""
    config = swarm(store, {"eng": {"agent": "codex"}})
    apply(store, config, [CLAUDE_FREE, CODEX_FULL], START)
    [found] = harness_pin.findings(store, SLUG, START + 5 * 60_000)
    assert found.summary == (
        "Lane eng is pinned to Codex, first seen 5m ago, while Claude has 3 free quota seats and 2 ready tasks wait"
    )
    assert found.evidence[0] == "The eng lane agent is codex, so capacity offers its tasks only Codex seats"


@pytest.mark.feature("swarm-harness-pin-guard")
@pytest.mark.parametrize(
    ("lanes", "rows", "max_eng", "tasks"),
    [
        ({"eng": {"agent": "auto"}}, [CLAUDE_FULL, CODEX_FREE], 4, ("e1", "e2")),
        ({}, [CLAUDE_FULL, CODEX_FREE], 4, ("e1", "e2")),
        ({"eng": {"agent": "claude"}}, [CLAUDE_FULL, CODEX_FULL], 4, ("e1", "e2")),
        ({"eng": {"agent": "claude"}}, [CLAUDE_FREE, CODEX_FREE], 4, ("e1", "e2")),
        ({"eng": {"agent": "claude"}}, [CLAUDE_FULL, CODEX_FREE], 0, ("e1", "e2")),
        ({"eng": {"agent": "claude"}}, [CLAUDE_FULL, CODEX_FREE], 4, ()),
    ],
    ids=["auto-lane", "no-lane-entry", "no-free-codex-seat", "claude-places-the-work", "lane-at-ceiling", "no-ready"],
)
def test_no_finding_without_a_pin_free_other_seats_or_waiting_work(
    store: RedisStore,
    lanes: dict[str, dict[str, str]],
    rows: list[capacity.Account],
    max_eng: int,
    tasks: tuple[str, ...],
) -> None:
    """A lane offered both harnesses, a full other harness, work the pinned harness places, a lane at its ceiling and
    a lane with no ready work raise nothing."""
    config = swarm(store, lanes, max_eng)
    apply(store, config, rows, START, tasks)
    assert harness_pin.findings(store, SLUG, START + DAY) == []


@pytest.mark.feature("swarm-harness-pin-guard")
@pytest.mark.parametrize(("effective", "waiting"), [(2, []), (1, ["e2"])], ids=["busy-at-ceiling", "one-below-ceiling"])
def test_waiting_tasks_list_only_below_the_lane_ceiling(effective: int, waiting: list[str]) -> None:
    """Busy agents at a ceiling above zero list no waiting task; one seat below it the unplaced task waits."""
    config = SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=0, max_plan=0, lanes={"eng": {"agent": "claude"}})
    decision = {
        "effective": {"eng": effective, "ci": 0, "plan": 0},
        "configured": {"eng": 2, "ci": 0, "plan": 0},
        "placeable": {"claude": 0, "codex": 4},
        "tasks": {"e1": "claude"},
    }
    ready = {"eng": [{"id": "e1"}, {"id": "e2"}], "ci": [], "plan": []}
    assert capacity.pins(config, decision, ready, {}, START) == {
        "pins": {"eng": {"agent": "claude", "since": START, "other": "codex", "free": 4, "waiting": waiting}}
    }


@pytest.mark.feature("swarm-harness-pin-guard")
def test_an_unpinned_lane_before_a_pinned_lane_with_no_ready_entry_still_stores_the_pin() -> None:
    """The scan passes an unpinned eng lane and stores the later ci pin; a lane absent from the ready work waits on
    nothing."""
    config = SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1, max_plan=0, lanes={"ci": {"agent": "codex"}})
    decision = {
        "effective": {"eng": 0, "ci": 0, "plan": 0},
        "configured": {"eng": 2, "ci": 1, "plan": 0},
        "placeable": {"claude": 3, "codex": 0},
        "tasks": {},
    }
    assert capacity.pins(config, decision, {"eng": [{"id": "e1"}]}, {}, START) == {
        "pins": {"ci": {"agent": "codex", "since": START, "other": "claude", "free": 3, "waiting": []}}
    }


@pytest.mark.feature("swarm-harness-pin-guard")
def test_the_pin_age_holds_while_the_pin_stands_and_restarts_when_it_changes(store: RedisStore) -> None:
    """The capacity pass keeps the time it first saw a pin until the pin changes or clears; no pin stores no key."""
    config = swarm(store, {"eng": {"agent": "claude"}})
    rows = [CLAUDE_FULL, CODEX_FREE]
    apply(store, config, rows, START)
    assert apply(store, config, rows, START + DAY)["pins"]["eng"]["since"] == START
    codex = replace(config, lanes={"eng": {"agent": "codex"}})
    assert apply(store, codex, rows, START + 2 * DAY)["pins"]["eng"]["since"] == START + 2 * DAY
    assert "pins" not in apply(store, replace(config, lanes={}), rows, START + 3 * DAY)
    assert apply(store, config, rows, START + 4 * DAY)["pins"] == {
        "eng": {"agent": "claude", "since": START + 4 * DAY, "other": "codex", "free": 6, "waiting": ["e1", "e2"]}
    }


@pytest.mark.feature("swarm-harness-pin-guard")
def test_the_status_quota_lines_name_each_pinned_lane(store: RedisStore) -> None:
    """Swarm status shows the pin, its age, the other harness's free seats and the waiting tasks under quota capacity."""
    config = swarm(store, {"eng": {"agent": "claude"}})
    apply(store, config, [CLAUDE_FULL, CODEX_FREE], START)
    decision = apply(store, config, [CLAUDE_FULL, CODEX_FREE], START + 6 * DAY)
    shown = quota_view.lines(decision, START + 6 * DAY, IDLE, MANUAL_SCALING)
    assert (
        shown[2]
        == "lane eng pinned to claude, first seen 6d00h ago: codex has 6 free quota seats and 2 ready tasks wait: e1, e2"
    )
    codex = replace(config, lanes={"eng": {"agent": "codex"}})
    placed = apply(store, codex, [CLAUDE_FULL, CODEX_FREE], START + 7 * DAY)
    assert quota_view.lines(placed, START + 7 * DAY, IDLE, MANUAL_SCALING)[2] == (
        "lane eng pinned to codex, first seen 0m ago: claude has 0 free quota seats and 0 ready tasks wait"
    )


@pytest.mark.feature("swarm-harness-pin-guard")
def test_findings_and_quota_lines_list_pinned_lanes_in_lane_name_order(store: RedisStore) -> None:
    """Pins stored eng first come back ci first in the findings and in the quota lines."""
    decision = apply(store, swarm(store, {}), [CLAUDE_FULL, CODEX_FREE], START)
    decision["pins"] = {
        "eng": {"agent": "claude", "since": START, "other": "codex", "free": 6, "waiting": ["e1"]},
        "ci": {"agent": "codex", "since": START, "other": "claude", "free": 1, "waiting": ["c1"]},
    }
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(decision))
    assert [found.subject for found in harness_pin.findings(store, SLUG, START)] == ["ci", "eng"]
    assert quota_view.lines(decision, START, IDLE, MANUAL_SCALING)[2:4] == [
        "lane ci pinned to codex, first seen 0m ago: claude has 1 free quota seats and 1 ready tasks wait: c1",
        "lane eng pinned to claude, first seen 0m ago: codex has 6 free quota seats and 1 ready tasks wait: e1",
    ]


@pytest.mark.feature("swarm-harness-pin-guard")
def test_swarm_status_lists_the_harness_pin_finding(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The swarm status findings carry the harness pin finding, aged at the status clock."""
    from scripts.swarm import status

    apply(store, swarm(store, {"eng": {"agent": "claude"}}), [CLAUDE_FULL, CODEX_FREE], START)
    monkeypatch.setattr(status, "now_ms", lambda: START + DAY)
    rows = status.findings(store, SLUG, store.config(SLUG), [], [])
    summary = (
        "Lane eng is pinned to Claude, first seen 1d00h ago, while Codex has 6 free quota seats and 2 ready tasks wait"
    )
    assert [(row["subject"], row["summary"]) for row in rows if row["kind"] == "harness pin"] == [("eng", summary)]
