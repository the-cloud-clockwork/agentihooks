"""Quota notice refresh returns the same actions, sends the same notices and writes the same decision for a pass with
working agents at hurry, handoff, healthy, unknown and missing accounts and stale quota readings."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, quota_notice
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW_MS = 1_000_000
ACCOUNTS = [
    capacity.Account("claude", "a", "OPEN", 1, 10, 80, 3, observed_at=1.0),
    capacity.Account("claude", "b", "OPEN", 1, 3, 50, 3, observed_at=1.0),
    capacity.Account("claude", "c", "OPEN", 1, 80, 80, 3, observed_at=1.0),
    capacity.Account("codex", "d", "UNKNOWN", 0, None, None, 0, observed_at=1.0),
    capacity.Account("codex", "e", "OPEN", 0, None, 12, 3, observed_at=1.0),
]
AGENTS = [
    AgentRecord("engineer@a1b2c3-0001", "eng", "t1", harness="claude", account="a", started_at=100),
    AgentRecord("engineer@a1b2c3-0002", "eng", "t2", harness="claude", account="b", started_at=200),
    AgentRecord("engineer@a1b2c3-0003", "eng", "t3", harness="claude", account="c", started_at=300),
    AgentRecord("ci@a1b2c3-0004", "ci", "t4", harness="codex", account="d", started_at=400),
    AgentRecord("engineer@a1b2c3-0005", "eng", "t5", harness="claude", account="z", started_at=500),
    AgentRecord("engineer@a1b2c3-0006", "eng", "t6", harness="codex", account="e", started_at=600),
    AgentRecord("engineer@a1b2c3-0007", "eng", "t7", harness="claude", account="a", state="starting", started_at=700),
]
READY = [
    {"id": "r1", "state": "open", "lane": "eng", "kind": "code", "difficulty": "M"},
    {"id": "r2", "state": "open", "lane": "ci", "kind": "ci", "difficulty": "M"},
]


CAPACITY = (
    "quota capacity eng 7 ci 2 plan 0 because accounts are unknown; Claude has 4 free seats and Codex has 3 free "
    "seats, claude b is at its five hour quota warning"
)
HURRY = "Your account has fifteen percent or less quota left. Finish the current step and push."
WARNING = (
    "QUOTA HANDOFF WARNING: claude account b has used 97% of its five hour window, 3% left; its reset time is "
    "unknown. Finish your current step and write your Handoff v2 with the handoff skill while quota remains. Submit "
    "it with agentihooks swarm sw handoff DOC --reason quota, then stop. The tick keeps your seat and task and routes "
    "the successor to an account with room. This warning does not block tools or terminate your running agent."
)
ACTIONS = [
    CAPACITY,
    "early quota handoff warning sent to engineer@a1b2c3-0002",
    "sent engineer@a1b2c3-0001 the quota hurry",
    "sent engineer@a1b2c3-0006 the quota hurry",
    "quota readings are 999 seconds old; the quota prober has not refreshed them",
]
SECOND_ACTIONS = ["quota readings are 1059 seconds old; the quota prober has not refreshed them"]
SENT = [("engineer@a1b2c3-0001", HURRY), ("engineer@a1b2c3-0002", WARNING), ("engineer@a1b2c3-0006", HURRY)]
NOTICES = {"engineer@a1b2c3-0001:100": "hurry", "engineer@a1b2c3-0006:600": "hurry"}
LIVES = {"engineer@a1b2c3-0002": "200"}
PERIODS = {"engineer@a1b2c3-0002:five hour": "200:None"}
COMMENTS = [(SLUG, "t1", CAPACITY)]
DECISION: dict[str, Any] = {
    "accounts": [
        {
            "cap": 3,
            "five_left": 10,
            "five_resets_at": None,
            "harness": "claude",
            "name": "a",
            "observed_at": 1.0,
            "sessions": 1,
            "state": "OPEN",
            "week_left": 80,
            "week_resets_at": None,
        },
        {
            "cap": 3,
            "five_left": 3,
            "five_resets_at": None,
            "harness": "claude",
            "name": "b",
            "observed_at": 1.0,
            "sessions": 1,
            "state": "OPEN",
            "week_left": 50,
            "week_resets_at": None,
        },
        {
            "cap": 3,
            "five_left": 80,
            "five_resets_at": None,
            "harness": "claude",
            "name": "c",
            "observed_at": 1.0,
            "sessions": 1,
            "state": "OPEN",
            "week_left": 80,
            "week_resets_at": None,
        },
        {
            "cap": 0,
            "five_left": None,
            "five_resets_at": None,
            "harness": "codex",
            "name": "d",
            "observed_at": 1.0,
            "sessions": 0,
            "state": "UNKNOWN",
            "week_left": None,
            "week_resets_at": None,
        },
        {
            "cap": 3,
            "five_left": None,
            "five_resets_at": None,
            "harness": "codex",
            "name": "e",
            "observed_at": 1.0,
            "sessions": 0,
            "state": "OPEN",
            "week_left": 12,
            "week_resets_at": None,
        },
    ],
    "allocation": {
        "ci": {"claude": 0, "codex": 1},
        "eng": {"claude": 1, "codex": 0},
        "plan": {"claude": 0, "codex": 0},
        "review": {"claude": 0, "codex": 0},
    },
    "at": 1000000,
    "autoscale": {
        "ceilings": {"ci": 2, "eng": 7, "plan": 0},
        "host": {
            "granted_at": 1000000,
            "held": False,
            "limit": "load",
            "reason": "one minute load 0.06 per CPU is below the low watermark; 2 live agents "
            "project load to the high watermark after 46 more",
            "room": 46,
        },
        "pending_raise": {"since": 1000000, "target": 14, "ticks": 1},
        "reason": "Quota seats 7 (claude 4, codex 3); host room 46; ceiling 9 of 14; capped by ready "
        "work eng 1, ci 1, plan 0.",
    },
    "configured": {"ci": 2, "eng": 7, "plan": 0, "review": 0},
    "effective": {"ci": 2, "eng": 7, "plan": 0, "review": 0},
    "host": {
        "granted_at": 1000000,
        "held": False,
        "limit": "load",
        "reason": "one minute load 0.06 per CPU is below the low watermark; 2 live agents project load to "
        "the high watermark after 46 more",
        "room": 46,
    },
    "observed_at": 1000000,
    "placeable": {"claude": 4, "codex": 3},
    "placements": {
        "ci": [{"account": "e", "harness": "codex", "index": 0}],
        "eng": [{"account": "a", "harness": "claude", "index": 0}],
        "plan": [],
        "review": [],
    },
    "reason": "accounts are unknown; Claude has 4 free seats and Codex has 3 free seats, claude b is at its "
    "five hour quota warning",
    "tasks": {"r1": "claude", "r2": "codex"},
}


@dataclass
class Refreshed:
    """One refresh's actions and the swarm state it left."""

    actions: list[str]
    store: RedisStore
    comments: list[tuple[str, str, str]]

    def hash(self, name: str) -> dict[str, str]:
        found: dict[str, str] = self.store.redis.hgetall(self.store.key(SLUG, name))  # pyright: ignore[reportAssignmentType]
        return found

    def sent(self) -> list[tuple[str, str]]:
        inbox = InboxStore(self.store.redis)
        return [(item.address, item.text) for agent in AGENTS for item in inbox.pending_items(agent.name)]

    def decision(self) -> dict[str, Any]:
        return capacity.read(self.store, SLUG)


def scenario(home: Path) -> tuple[RedisStore, Any, HerdrRuntime, list[tuple[str, str, str]]]:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1, max_plan=0, code="a1b2c3"))
    for agent in AGENTS:
        store.put_agent(SLUG, agent)
    claimed = [{"id": agent.task, "state": "claimed", "lane": agent.lane, "claimed_by": agent.name} for agent in AGENTS]
    ledger: Any = FakeLedger(claimed + [dict(row) for row in READY])
    comments: list[tuple[str, str, str]] = []
    ledger.comment = lambda slug, item, text, by: comments.append((slug, item, text))
    runtime = HerdrRuntime(home=home)
    runtime.quota_accounts = lambda environ, now, refresh=False: list(ACCOUNTS)
    return store, ledger, runtime, comments


def refreshed(home: Path, passes: int = 1) -> Refreshed:
    store, ledger, runtime, comments = scenario(home)
    actions: list[str] = []
    for index in range(passes):
        actions = quota_notice.refresh(SLUG, store.config(SLUG), store, ledger, runtime, NOW_MS + index * 60_000)
    return Refreshed(actions, store, comments)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.feature("tick-quota-thin-cpu")
def test_refresh_returns_the_pinned_actions_for_mixed_agents(home: Path) -> None:
    assert refreshed(home).actions == ACTIONS


@pytest.mark.feature("tick-quota-thin-cpu")
def test_refresh_sends_the_pinned_notices_and_records_each_life(home: Path) -> None:
    found = refreshed(home)
    assert found.sent() == SENT
    assert found.hash("quota-notices") == NOTICES
    assert found.hash("quota-warning-lives") == LIVES
    assert found.hash("quota-warning-periods") == PERIODS


@pytest.mark.feature("tick-quota-thin-cpu")
def test_refresh_writes_the_pinned_decision(home: Path) -> None:
    found = refreshed(home)
    assert json.loads(json.dumps(found.decision())) == DECISION
    assert found.comments == COMMENTS


@pytest.mark.feature("tick-quota-thin-cpu")
def test_a_second_refresh_sends_nothing_new(home: Path) -> None:
    found = refreshed(home, passes=2)
    assert found.actions == SECOND_ACTIONS
    assert found.sent() == SENT
