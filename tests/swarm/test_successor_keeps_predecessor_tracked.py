"""A successor started on a seat keeps its finished predecessor tracked until the tick reap ends its process."""

import json
from dataclasses import replace
from typing import Any

import pytest

from scripts.swarm import master_launch, seat_heads, session_model, tick
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit

SLUG = "sw"
SEAT = f"{MASTER}@{SLUG}"
NOW = 1_800_000_000_000


class _Ledger:
    """A ledger with no task rows."""

    def update_task(self, slug: str, task_id: str, fields: dict[str, Any], **guards: Any) -> dict[str, Any]:
        """Answer the task unchanged."""
        return {}

    def task(self, slug: str, task_id: str) -> dict[str, Any]:
        """Answer no live row."""
        return {}


class _Retiring:
    """A runtime that ends every process it is asked to retire and records each agent it ended."""

    def __init__(self) -> None:
        self.retired: list[AgentRecord] = []

    def live_names(self) -> set[str]:
        """Answer no live process."""
        return set()

    def observe(self, agent: AgentRecord) -> PaneObservation:
        """Answer that the pane has exited."""
        return PaneObservation("exited")

    def retire(self, agent: AgentRecord, homes: Any = ()) -> bool:
        """Record the retired agent and report its process ended."""
        self.retired.append(agent)
        return True

    def refusal(self, agent: AgentRecord) -> dict[str, Any]:
        """Name no refusal."""
        return {}


@pytest.fixture
def store() -> RedisStore:
    """Return a fakeredis swarm store for one running swarm."""
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    result.create(SwarmConfig(SLUG, ".", 2, 0, state="running"))
    return result


def _handed_off(store: RedisStore, **placement: Any) -> tuple[AgentRecord, AgentRecord]:
    """Start a master, finish it as a handoff does, then start its successor on the same seat."""
    first = AgentRecord(store.next_name(SLUG, MASTER), MASTER, MASTER, seat=SEAT, started_at=NOW - 2, **placement)
    started = store.start_execution(SLUG, first)
    finished = replace(started, state="finished")
    store.put_agent(SLUG, finished)
    successor = AgentRecord(store.next_name(SLUG, MASTER), MASTER, MASTER, seat=SEAT, started_at=NOW - 1)
    return finished, store.start_execution(SLUG, successor, started.execution_id)


@pytest.mark.feature("swarm-successor-keeps-predecessor-tracked")
def test_a_successor_start_keeps_its_finished_predecessor_listed(store: RedisStore) -> None:
    finished, successor = _handed_off(store)

    listed = {agent.name: agent for agent in store.agents(SLUG)}

    assert set(listed) == {finished.name, successor.name}
    assert listed[finished.name].state == "finished"
    assert listed[finished.name].execution_id == finished.execution_id


@pytest.mark.feature("swarm-successor-keeps-predecessor-tracked")
def test_archiving_keeps_the_placement_of_a_listed_predecessor(store: RedisStore) -> None:
    finished, _ = _handed_off(store, hive="h1", runtime_target={"host_id": "h1"})

    store.execution_registry.archive(SLUG)
    listed = {agent.name: agent for agent in store.agents(SLUG)}

    assert listed[finished.name].execution_id == finished.execution_id
    assert listed[finished.name].hive == "h1"
    assert listed[finished.name].runtime_target == {"host_id": "h1"}


@pytest.mark.feature("swarm-successor-keeps-predecessor-tracked")
def test_the_reap_ends_a_handed_off_predecessor_and_drops_it_with_history(store: RedisStore) -> None:
    finished, successor = _handed_off(store)
    runtime = _Retiring()
    judging = tick._Reaping(SLUG, store, _Ledger(), runtime, {}, set(), NOW)

    actions = [
        action
        for agent in store.agents(SLUG)
        if agent.name == finished.name
        for action in agents._reap_agent(judging, agent)
    ]

    assert actions == [f"retired {finished.name}"]
    assert [agent.execution_id for agent in runtime.retired] == [finished.execution_id]
    assert {agent.name for agent in store.agents(SLUG)} == {successor.name}
    history = [json.loads(row) for row in store.redis.lrange(store.key(SLUG, "history"), 0, -1)]
    assert [row["name"] for row in history] == [finished.name]
    assert store.names.entry(finished.name)["retired_at"]


@pytest.mark.feature("swarm-successor-keeps-predecessor-tracked")
def test_the_session_models_step_passes_over_a_listed_finished_predecessor(store: RedisStore) -> None:
    finished, successor = _handed_off(store)
    for agent in (finished, successor):
        session_model.put(store.redis, SLUG, agent.name, "claude-opus-5-5", "xhigh", NOW)

    agents._session_models(SLUG, store)

    listed = {agent.name: agent for agent in store.agents(SLUG)}
    assert listed[finished.name].model == finished.model
    assert listed[successor.name].model == "claude-opus-5-5"


@pytest.mark.feature("swarm-successor-keeps-predecessor-tracked")
def test_a_listed_finished_predecessor_is_no_front_door(store: RedisStore) -> None:
    finished, successor = _handed_off(store)

    front = master_launch._front_doors(store, SLUG, {finished.name, successor.name})

    assert [agent.name for agent in front] == [successor.name]


@pytest.mark.feature("swarm-successor-keeps-predecessor-tracked")
def test_only_a_finished_agent_with_a_newer_listed_execution_on_its_seat_is_superseded(store: RedisStore) -> None:
    finished, successor = _handed_off(store)
    listed = store.agents(SLUG)

    assert [seat_heads.superseded(agent, listed) for agent in (finished, successor)] == [True, False]
    assert seat_heads.superseded(finished, [finished]) is False
    assert seat_heads.superseded(replace(finished, state="working"), listed) is False
    assert seat_heads.superseded(replace(finished, seat=f"{MASTER}-2@{SLUG}"), listed) is False
