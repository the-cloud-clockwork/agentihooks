import copy
import json
from collections.abc import Callable, Iterator
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from scripts.tick import view

STATE: dict[str, Any] = {
    "tasks": [{"id": "t1", "state": "open", "territory": ["scripts/tick/"]}],
    "_meta": {"rev": 4, "stamps": {"a": {"by": "operator"}}},
    "phases": [{"id": "p1", "done": False}],
}


@pytest.fixture
def ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")


@pytest.fixture
def held() -> Iterator[None]:
    token = view.HELD.set(None)
    current = view.CURRENT.set(None)
    yield
    view.HELD.reset(token)
    view.CURRENT.reset(current)


def refused(change: Callable[[], object]) -> str:
    with pytest.raises(TypeError) as raised:
        change()
    return str(raised.value)


@pytest.mark.feature("swarm-tick-no-shared-rows")
@pytest.mark.usefixtures("ci")
def test_a_step_mutating_the_view_fails_under_ci() -> None:
    state = copy.deepcopy(STATE)
    seen = view.LedgerView(3, state)
    task = seen.tasks[0]
    changes: list[Callable[[], object]] = [
        lambda: task.__setitem__("state", "claimed"),
        lambda: task.update(state="claimed"),
        lambda: task.pop("state"),
        lambda: task.setdefault("x", 1),
        lambda: task.__delitem__("id"),
        lambda: task.clear(),
        lambda: task.popitem(),
        lambda: task.__ior__({"x": 1}),
        lambda: task["territory"].append("x"),
        lambda: task["territory"].__setitem__(0, "x"),
        lambda: task["territory"].extend(["x"]),
        lambda: task["territory"].insert(0, "x"),
        lambda: task["territory"].remove("scripts/tick/"),
        lambda: task["territory"].pop(),
        lambda: task["territory"].sort(),
        lambda: task["territory"].reverse(),
        lambda: task["territory"].clear(),
        lambda: task["territory"].__delitem__(0),
        lambda: task["territory"].__iadd__(["x"]),
        lambda: task["territory"].__imul__(2),
        lambda: seen.meta["stamps"]["a"].__setitem__("by", "swarm"),
        lambda: seen.phases.append({"id": "p2"}),
        lambda: seen.document.__setitem__("tasks", []),
    ]
    assert {refused(change) for change in changes} == {view.READ_ONLY}
    assert seen.tasks == STATE["tasks"]
    assert seen.read("tasks", "t1") == STATE["tasks"][0]
    assert state == STATE


@pytest.mark.feature("swarm-tick-no-shared-rows")
def test_outside_ci_the_view_holds_the_state_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "")
    state = copy.deepcopy(STATE)
    seen = view.LedgerView(3, state)
    assert seen.document is state
    assert seen.tasks is state["tasks"]
    assert type(seen.tasks) is list


@pytest.mark.parametrize(("value", "frozen"), [("1", True), ("TRUE", True), ("yes", True), ("false", False)])
def test_ci_settings_that_freeze_views(monkeypatch: pytest.MonkeyPatch, value: str, frozen: bool) -> None:
    monkeypatch.setenv("CI", value)
    assert view.frozen_views() is frozen


def test_no_ci_setting_leaves_views_unfrozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    assert view.frozen_views() is False


def test_a_frozen_view_copies_and_serialises_as_plain_values() -> None:
    frozen = view.freeze(STATE)
    assert type(frozen) is view.FrozenDict
    assert frozen == STATE
    assert json.loads(json.dumps(frozen)) == STATE
    for copied in (copy.deepcopy(frozen), copy.copy(frozen)):
        assert type(copied) is dict
        assert copied == STATE
    plain = copy.deepcopy(frozen)
    assert type(plain["tasks"]) is list
    assert type(plain["tasks"][0]["territory"]) is list
    plain["tasks"].append({"id": "t2"})
    assert len(frozen["tasks"]) == 1
    assert type(copy.copy(frozen["tasks"])) is list
    assert view.freeze(7) == 7
    assert view.freeze("open") == "open"


@pytest.mark.feature("swarm-tick-no-shared-rows")
@pytest.mark.usefixtures("held")
def test_a_ledger_write_makes_the_next_step_read_a_fresh_view() -> None:
    first, second = view.LedgerView(1, {"tasks": []}), view.LedgerView(2, {"tasks": [{"id": "t1"}]})
    renewed: list[view.LedgerView] = []

    def renew() -> view.LedgerView:
        renewed.append(second)
        return second

    view.HELD.set(view.Held(first, renew))
    assert view.current() is first
    assert view.current() is first
    assert renewed == []
    view.wrote()
    assert view.current() is second
    assert view.current() is second
    assert renewed == [second]


@pytest.mark.usefixtures("held")
def test_without_a_held_view_current_is_the_pass_view_and_a_write_is_harmless() -> None:
    seen = view.LedgerView(1, {})
    view.CURRENT.set(seen)
    view.wrote()
    assert view.current() is seen


@pytest.mark.feature("swarm-tick-no-shared-rows")
@pytest.mark.usefixtures("held")
def test_a_ledger_client_write_marks_the_held_view_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import ledger_client

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: {}))
    stale = view.Held(view.LedgerView(1, {}), lambda: view.LedgerView(2, {}))
    view.HELD.set(stale)
    ledger_client.LedgerClient().state("sw")
    assert stale.stale is False
    ledger_client.LedgerClient().join("sw", "engineer@a1b2c3-0001", "member")
    assert stale.stale is True


@pytest.mark.feature("swarm-tick-no-shared-rows")
def test_the_pass_hands_steps_rows_no_step_can_assign_into(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import tick

    doc = {"tasks": [{"id": "t1", "state": "open"}]}
    monkeypatch.setattr(tick.freeze, "watched", lambda slug, store, ledger, state: state)
    box: dict[str, Any] = {}
    assert (
        tick.step_ledger_state("sw", cast(Any, SimpleNamespace()), SimpleNamespace(state=lambda slug: doc), box) == []
    )
    rows = box["rows"]
    assert type(rows) is MappingProxyType
    assert dict(rows) == {"t1": doc["tasks"][0]}
    assert box["doc"] is doc
    with pytest.raises(TypeError):
        cast(Any, rows)["t1"] = {"id": "t1", "state": "claimed"}
