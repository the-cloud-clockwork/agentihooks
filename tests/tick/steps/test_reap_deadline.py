"""The reap step hands the reap pass the pass runtime, ledger and its deadline, and the pass stops at that deadline."""

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from scripts.hub import reconciler
from scripts.swarm import tick
from scripts.swarm.store import AgentRecord
from scripts.tick.adapters.calls import LedgerCalls, RuntimeCalls
from scripts.tick.adapters.fakes import fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit

LEFT = "reap stopped at the pass deadline, {left} of {total} agents left for the next pass"


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float) -> None:
        """Start the clock at ``now``."""
        self.now = now

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


class _Runtime:
    """A runtime listing no live agent."""

    def live_names(self) -> set[str]:
        """Return no live agent names."""
        return set()


def _sequential(action: Callable[[Any], Any], items: Iterable[Any]) -> list[Any]:
    """Run ``action`` on each item in order, as a sequential pass does."""
    return [action(item) for item in items]


def _agents(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """Make the reap pass see one working agent per name, each on its own task."""
    found = [AgentRecord(name=name, lane="eng", task=f"task-{name}", state="working") for name in names]
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: found)
    monkeypatch.setattr(tick.pool, "fan_out", _sequential)


def _judging(monkeypatch: pytest.MonkeyPatch, clock: _Clock, step_s: float) -> list[str]:
    """Record each agent judged, moving ``clock`` by ``step_s`` per judgement."""
    judged: list[str] = []

    def judge(judging: Any, agent: AgentRecord) -> list[str]:
        """Record the agent and spend ``step_s`` of the clock."""
        judged.append(agent.name)
        clock.now += step_s
        return [f"judged {agent.name}"]

    monkeypatch.setattr(tick, "_reap_agent", judge)
    return judged


def test_a_reap_pass_past_its_deadline_stops_and_leaves_every_agent_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    _agents(monkeypatch, ["a", "b"])
    judged = _judging(monkeypatch, clock, 0.0)

    actions = agents._reap("sw", object(), object(), _Runtime(), {}, 1, Deadline(100.0, clock))

    assert judged == []
    assert actions == [LEFT.format(left=2, total=2)]


def test_a_reap_pass_whose_deadline_expires_midway_leaves_the_rest_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(0.0)
    _agents(monkeypatch, ["a", "b", "c"])
    judged = _judging(monkeypatch, clock, 5.0)

    actions = agents._reap("sw", object(), object(), _Runtime(), {}, 1, Deadline(4.0, clock))

    assert judged == ["a"]
    assert actions == ["judged a", LEFT.format(left=2, total=3)]


def test_a_reap_pass_inside_its_deadline_judges_every_agent_with_no_stop_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(0.0)
    _agents(monkeypatch, ["a", "b"])
    judged = _judging(monkeypatch, clock, 1.0)

    actions = agents._reap("sw", object(), object(), _Runtime(), {}, 1, Deadline(10.0, clock))

    assert judged == ["a", "b"]
    assert actions == ["judged a", "judged b"]


def test_a_reap_pass_with_no_deadline_judges_every_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _agents(monkeypatch, ["a", "b"])
    judged = _judging(monkeypatch, _Clock(0.0), 1.0)

    assert agents._reap("sw", object(), object(), _Runtime(), {}, 1) == ["judged a", "judged b"]
    assert judged == ["a", "b"]


def test_a_reap_pass_returns_actions_in_agent_order_when_tasks_interleave(monkeypatch: pytest.MonkeyPatch) -> None:
    found = [
        AgentRecord(name="a", lane="eng", task="x", state="working"),
        AgentRecord(name="b", lane="eng", task="y", state="working"),
        AgentRecord(name="c", lane="eng", task="x", state="working"),
    ]
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: found)
    monkeypatch.setattr(tick.pool, "fan_out", _sequential)
    judged = _judging(monkeypatch, _Clock(0.0), 1.0)

    actions = agents._reap("sw", object(), object(), _Runtime(), {}, 1, Deadline(10.0, _Clock(0.0)))

    assert judged == ["a", "c", "b"]
    assert actions == ["judged a", "judged b", "judged c"]


class _View:
    """A pass view answering the pass state."""

    def __init__(self, state: dict[str, Any]) -> None:
        """Keep the pass state."""
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        """Return the pass value under ``key``."""
        assert domain == Domain("pass")
        return self.state.get(key)


def test_the_reap_step_hands_the_pass_runtime_ledger_and_its_deadline_to_the_reap_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, ...]] = []

    def reap(*args: Any) -> list[str]:
        """Record what the step handed the reap pass."""
        seen.append(args)
        return ["reaped"]

    monkeypatch.setattr(agents, "_reap", reap)
    store, ledger, runtime, rows = object(), object(), _Runtime(), {"t1": {}}
    state = {"slug": "sw", "store": store, "ledger": ledger, "runtime": runtime, "rows": rows, "now_ms": 7}
    deadline = Deadline(50.0, _Clock(0.0))
    step = next(found for found in agents.STEPS if found.name == "reap")

    tools = fake_tools()
    result = step.run(Context(_View(state), deadline, tools))

    assert seen == [
        ("sw", store, LedgerCalls(tools.ledger, deadline), RuntimeCalls(tools.runtime, deadline), rows, 7, deadline)
    ]
    assert [write.value for write in result.writes] == [["reaped"]]
