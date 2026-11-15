"""Direct coverage of failed_panes._tail, _close and sweep's collection, bypassing the full tick pass."""

import json

import fakeredis
import pytest

from scripts.swarm import failed_panes
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

SLUG = "sw"
FAILED_AT = 1_000
DUE = FAILED_AT + failed_panes.CLOSE_AFTER_MS


class _Runtime:
    def __init__(
        self,
        screens: dict[str, str] | None = None,
        owned: set[str] | None = None,
        live: set[str] | None = None,
        tail_error: str = "",
        launched_error: str = "",
        close_error: str = "",
    ) -> None:
        self.screens: dict[str, str] = dict(screens or {})
        self.owned: set[str] = set(self.screens) if owned is None else set(owned)
        self.live: set[str] = set(live or set())
        self.closed: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.tail_error, self.launched_error, self.close_error = tail_error, launched_error, close_error

    def live_names(self) -> set[str]:
        return self.live

    def pane_tail(self, pane_id: str, lines: int) -> str:
        if self.tail_error:
            raise RuntimeError(self.tail_error)
        return self.screens[pane_id]

    def launched_pane(self, pane_id: str, name: str) -> bool:
        self.asked.append((pane_id, name))
        if self.launched_error:
            raise RuntimeError(self.launched_error)
        return pane_id in self.owned

    def close_pane(self, pane_id: str) -> None:
        if self.close_error:
            raise RuntimeError(self.close_error)
        self.closed.append(pane_id)
        self.screens.pop(pane_id, None)


@pytest.fixture
def store() -> RedisStore:
    redis_store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    redis_store.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=0))
    return redis_store


def _held(store: RedisStore, agent: AgentRecord) -> None:
    failed_panes.hold(store, SLUG, agent, FAILED_AT, "boom")


def _launched(store: RedisStore, agent: AgentRecord) -> None:
    row = {"agent": agent.name, "task": agent.task, "pane_id": agent.pane_id, "error": ""}
    store.redis.hset(store.key(SLUG, "launches"), agent.name, json.dumps(row))


def _noted(store: RedisStore, agent: AgentRecord) -> tuple[str, list[str]]:
    row = json.loads(store.redis.hget(store.key(SLUG, "launches"), agent.name))
    return row["error"], row["pane_lines"]


def test_sweep_keeps_the_pane_open_before_close_after_ms_passes(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0001", "eng", "t1", pane_id="w1:p1")
    _held(store, agent)
    runtime = _Runtime({"w1:p1": "one\ntwo\n"})

    assert failed_panes.sweep(SLUG, store, runtime, DUE - 1) == []

    assert runtime.closed == []
    assert "w1:p1" in failed_panes.held(store, SLUG)


def test_sweep_closes_the_pane_once_due_and_keeps_its_last_lines(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0001", "eng", "t1", pane_id="w1:p1")
    _held(store, agent)
    _launched(store, agent)
    runtime = _Runtime({"w1:p1": "one\ntwo  \n\n\n"})

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    assert actions == [f"closed pane w1:p1 of the failed launch of {agent.name} for t1"]
    assert runtime.closed == ["w1:p1"]
    assert runtime.asked == [("w1:p1", agent.name)]
    assert failed_panes.held(store, SLUG) == {}
    assert _noted(store, agent) == ("boom", ["one", "two"])


def test_a_pane_whose_lines_cannot_be_read_still_closes_and_says_so(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0002", "eng", "t1", pane_id="w1:p2")
    _held(store, agent)
    _launched(store, agent)
    runtime = _Runtime({}, owned={"w1:p2"}, tail_error="pane w1:p2 not found")

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    assert actions == [f"closed pane w1:p2 of the failed launch of {agent.name} for t1"]
    assert runtime.closed == ["w1:p2"]
    assert _noted(store, agent) == ("boom", ["its lines could not be read: pane w1:p2 not found"])


def test_another_agent_now_holding_the_pane_is_kept_and_dropped_from_held(store: RedisStore) -> None:
    gone = AgentRecord("engineer@a1-0003", "eng", "t1", pane_id="w1:p3")
    _held(store, gone)
    holder = AgentRecord("engineer@a1-0004", "eng", "t2", pane_id="w1:p3")
    store.put_agent(SLUG, holder)
    runtime = _Runtime({"w1:p3": "x\n"})

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    assert actions == [f"kept pane w1:p3: {holder.name} holds it now"]
    assert runtime.closed == []
    assert failed_panes.held(store, SLUG) == {}


def test_a_live_agent_pane_is_left_alone(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0005", "eng", "t1", pane_id="w1:p5")
    _held(store, agent)
    runtime = _Runtime({"w1:p5": "x\n"}, live={agent.name})

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    assert actions == []
    assert runtime.closed == []
    assert list(failed_panes.held(store, SLUG)) == ["w1:p5"]


def test_a_pane_herdr_cannot_list_stays_held_for_the_next_tick(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0006", "eng", "t1", pane_id="w1:p6")
    _held(store, agent)
    runtime = _Runtime({"w1:p6": "x\n"}, launched_error="server down")

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    expected = f"could not read pane w1:p6 of the failed launch of {agent.name}, retrying next tick: server down"
    assert actions == [expected]
    assert list(failed_panes.held(store, SLUG)) == ["w1:p6"]
    assert runtime.closed == []


def test_a_pane_no_longer_shown_by_herdr_is_dropped_untouched(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0007", "eng", "t1", pane_id="w1:p7")
    _held(store, agent)
    runtime = _Runtime({"w1:p7": "x\n"}, owned=set())

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    assert actions == [f"dropped pane w1:p7: herdr no longer shows the failed launch of {agent.name} there"]
    assert failed_panes.held(store, SLUG) == {}


def test_a_pane_herdr_refuses_to_close_stays_held_for_the_next_tick(store: RedisStore) -> None:
    agent = AgentRecord("engineer@a1-0008", "eng", "t1", pane_id="w1:p8")
    _held(store, agent)
    runtime = _Runtime({"w1:p8": "x\n"}, close_error="server down")

    actions = failed_panes.sweep(SLUG, store, runtime, DUE)

    expected = f"could not close pane w1:p8 of the failed launch of {agent.name}, retrying next tick: server down"
    assert actions == [expected]
    assert list(failed_panes.held(store, SLUG)) == ["w1:p8"]
