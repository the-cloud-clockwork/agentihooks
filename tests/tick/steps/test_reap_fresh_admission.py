"""The reap step keeps a fresh hub admission whose pass row predates the hub's ledger claim, and still ends one that
lost its claim."""

from dataclasses import replace
from typing import Any

import pytest

from scripts.swarm import tick
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW = 1_800_000_000_000
UNCLAIMED = {"id": "t1", "state": "open", "claimed_by": "", "claim_generation": 0}


class _Ledger:
    """A ledger that records each task update and answers the task reopened."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update_task(self, slug: str, task_id: str, fields: dict[str, Any], **guards: Any) -> dict[str, Any]:
        """Record the update and answer the task reopened."""
        self.updates.append((task_id, fields))
        return {"state": "open", "claimed_by": ""}

    def task(self, slug: str, task_id: str) -> dict[str, Any]:
        """Answer no live row."""
        return {}


class _Gone:
    """A runtime that sees no process for any agent and retires each at once."""

    def live_names(self) -> set[str]:
        """Answer no live process."""
        return set()

    def observe(self, agent: AgentRecord) -> PaneObservation:
        """Answer that the runtime did not see the agent."""
        return PaneObservation("unknown")

    def retire(self, agent: AgentRecord, homes: Any = ()) -> bool:
        """Answer the agent retired."""
        return True

    def refusal(self, agent: AgentRecord) -> dict[str, Any]:
        """Answer no refusal."""
        return {}

    def reported(self, agent: AgentRecord) -> bool:
        """Answer no hive report for the agent."""
        return False


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    """Return a fakeredis swarm store judged from a hive other than the one the hub admitted to."""
    import fakeredis

    monkeypatch.setenv("SWARM_HIVE_ID", "workstation")
    result = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    result.create(SwarmConfig(SLUG, ".", 2, 0, state="running"))
    return result


def _admitted(store: RedisStore) -> AgentRecord:
    """Claim task t1 and start its starting, unlaunched execution on hive-a, as a hub admission does before its
    ledger claim write lands."""
    name = store.next_name(SLUG, "eng")
    assert store.claim(SLUG, "t1", name, tick.LEASE_MS)
    record = AgentRecord(name, "eng", "t1", seat=f"eng-1@{SLUG}", state="starting", started_at=NOW, hive="hive-a")
    return store.start_execution(SLUG, record)


def _reap(store: RedisStore, row: dict[str, Any], agent: AgentRecord) -> list[str]:
    """Judge one agent in a reap pass whose rows hold ``row`` for task t1."""
    judging = tick._Reaping(SLUG, store, _Ledger(), _Gone(), {"t1": dict(row)}, set(), NOW + 25_000)
    return agents._reap_agent(judging, agent)


@pytest.mark.feature("hub-late-admission-retry-launches")
def test_a_fresh_hub_admission_judged_on_a_row_read_before_its_ledger_claim_keeps_its_claim(
    store: RedisStore,
) -> None:
    agent = _admitted(store)

    actions = _reap(store, UNCLAIMED, agent)

    assert f"retired {agent.name}" not in actions
    assert agent.name in store.listed(SLUG)
    assert store.claimant(SLUG, "t1") == agent.name


@pytest.mark.feature("hub-late-admission-retry-launches")
@pytest.mark.parametrize(
    "row",
    [
        {**UNCLAIMED, "state": "claimed", "claimed_by": "engineer@another"},
        {**UNCLAIMED, "state": "blocked"},
        {**UNCLAIMED, "state": "done", "done": True},
        {**UNCLAIMED, "state": "handoff"},
    ],
    ids=["another-claimant", "blocked", "done", "handoff"],
)
def test_a_fresh_hub_admission_still_ends_on_a_row_another_claimant_or_a_closed_state_holds(
    store: RedisStore, row: dict[str, Any]
) -> None:
    agent = _admitted(store)

    assert _reap(store, row, agent) == [f"retired {agent.name}"]
    assert agent.name not in store.listed(SLUG)
    assert store.claimant(SLUG, "t1") is None


@pytest.mark.feature("hub-late-admission-retry-launches")
@pytest.mark.parametrize("change", ["released", "launched", "launch-recorded", "suspect"])
def test_an_agent_past_its_fresh_admission_still_ends_on_an_unclaimed_row(store: RedisStore, change: str) -> None:
    agent = _admitted(store)
    if change == "released":
        assert store.release(SLUG, "t1", agent.name)
    else:
        moved = {
            "launched": {"state": "working", "launched_at": NOW + 1},
            "launch-recorded": {"launched_at": NOW + 1},
            "suspect": {"state": tick.SUSPECT},
        }
        agent = replace(agent, **moved[change])
        store.put_agent(SLUG, agent)

    assert _reap(store, UNCLAIMED, agent) == [f"retired {agent.name}"]
    assert agent.name not in store.listed(SLUG)
    assert store.claimant(SLUG, "t1") is None
