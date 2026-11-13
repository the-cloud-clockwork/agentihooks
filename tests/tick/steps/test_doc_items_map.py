"""The doc items map tick step maps a changed watchdog document copy onto its tasks once per change."""

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from time import sleep
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import doc_items
from scripts.tick import engine, pool, registry
from scripts.tick.adapters.fakes import FakeCoordination, FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import CallStep, pass_domains

pytestmark = pytest.mark.unit

COPY = """# Open work
1. [open] First item. [Ledger: t1]
2. [closed] Finished item. [Ledger: t2]
3. [open] Range item. [Ledger: t1 to t2]
"""


def _step() -> CallStep:
    import_module("scripts.tick.steps.report")
    found = {step.name: step for step in registry.steps()}["doc items map"]
    assert isinstance(found, CallStep)
    return found


def _values(
    copy: str | None, tasks: list[dict[str, Any]], marked: str | None = None, rev: int | None = 7
) -> dict[str, Any]:
    tools = fake_tools()
    assert isinstance(tools.coordination, FakeCoordination)
    assert isinstance(tools.ledger, FakeLedger)
    tools.coordination.answers["marked"] = marked
    tools.ledger.answers["watchdog_copy"] = {} if copy is None else {"text": copy, "digest": doc_items.digest(copy)}
    stamps = {} if rev is None else {doc_items.COPY_STAMP: {"rev": rev, "by": "watchdog"}}
    return {
        "doc": {"tasks": tasks, "_meta": {"stamps": stamps}},
        "tools": tools.for_effects(frozenset({"ledger", "coordination"})),
        "deadline": Deadline.after(5),
        "fakes": tools,
    }


def _tasks() -> list[dict[str, Any]]:
    return [{"id": "t1", "doc_items": ["3 Range item."]}, {"id": "t2"}]


def _ledger(values: dict[str, Any]) -> FakeLedger:
    ledger = values["fakes"].ledger
    assert isinstance(ledger, FakeLedger)
    return ledger


def _writes(values: dict[str, Any]) -> list[tuple[str, tuple[object, ...]]]:
    calls = [(call.operation, call.args) for call in _ledger(values).calls]
    assert calls[0] == ("watchdog_copy", ())
    return calls[1:]


def _coordination(values: dict[str, Any]) -> list[tuple[str, tuple[object, ...]]]:
    coordination = values["fakes"].coordination
    assert isinstance(coordination, FakeCoordination)
    return [(call.operation, call.args) for call in coordination.calls]


READ = ("marked", (doc_items.REVISION_MARK,))


def _seen(rev: int = 7) -> tuple[str, tuple[object, ...]]:
    return ("mark", (doc_items.REVISION_MARK, str(rev)))


@pytest.mark.feature("tick-doc-items-map")
def test_doc_items_map() -> None:
    """The doc items map step registers in group wrapper events on the slow lane with its feature and ports."""
    found = _step()
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper events",
        "slow",
        "tick-doc-items-map",
        frozenset({"coordination", "ledger"}),
    )
    assert found.reads == pass_domains("doc")
    assert found.needs == frozenset({"ledger state", "drained resume", "tick view"})


@pytest.mark.feature("tick-doc-items-map")
def test_a_new_copy_on_the_ledger_maps_its_items_to_their_tasks_exactly_once() -> None:
    values = _values(COPY, _tasks())
    assert _step().call(None, values.__getitem__) == [
        "mapped watchdog doc items of the ledger copy: 3 items on 2 tasks, updated t1, t2"
    ]
    assert _writes(values) == [
        ("update_task", ("t1", {"doc_items": ["3 Range item.", "1 First item."]}, (), None)),
        ("update_task", ("t2", {"doc_items": ["3 Range item."]}, (), None)),
    ]
    assert _coordination(values) == [
        READ,
        ("marked", (doc_items.MARK,)),
        ("mark", (doc_items.MARK, doc_items.digest(COPY))),
        _seen(),
    ]
    again = _values(COPY, _tasks(), "7")
    assert _step().call(None, again.__getitem__) == []
    assert _ledger(again).calls == []
    assert _coordination(again) == [READ]
    newer = COPY + "4. [open] New item. [Ledger: t2]\n"
    changed = _values(newer, _tasks(), "7", rev=9)
    assert _step().call(None, changed.__getitem__) == [
        "mapped watchdog doc items of the ledger copy: 4 items on 2 tasks, updated t1, t2"
    ]
    assert [args for _, args in _writes(changed)] == [
        ("t1", {"doc_items": ["3 Range item.", "1 First item."]}, (), None),
        ("t2", {"doc_items": ["3 Range item.", "4 New item."]}, (), None),
    ]
    assert _coordination(changed)[-2:] == [("mark", (doc_items.MARK, doc_items.digest(newer))), _seen(9)]


@pytest.mark.feature("tick-doc-items-map")
def test_an_unchanged_copy_runs_nothing() -> None:
    values = _values(COPY, _tasks(), doc_items.digest(COPY))
    assert _step().call(None, values.__getitem__) == []
    assert _writes(values) == []
    assert _coordination(values) == [READ, ("marked", (doc_items.MARK,)), _seen()]


@pytest.mark.feature("tick-doc-items-map")
@pytest.mark.parametrize("copy", [None, ""])
def test_a_ledger_without_a_copy_runs_nothing(copy: str | None) -> None:
    values = _values(copy, _tasks())
    assert _step().call(None, values.__getitem__) == []
    assert _writes(values) == []
    assert _coordination(values) == [READ, _seen()]


@pytest.mark.feature("tick-doc-items-map")
def test_a_ledger_view_without_a_stored_copy_reads_nothing() -> None:
    values = _values(COPY, _tasks(), rev=None)
    assert _step().call(None, values.__getitem__) == []
    assert _ledger(values).calls == []
    assert _coordination(values) == []


@pytest.mark.feature("tick-doc-items-map")
def test_a_copy_already_on_its_tasks_records_its_digest_without_writes() -> None:
    tasks = [
        {"id": "t1", "doc_items": ["1 First item.", "3 Range item."]},
        {"id": "t2", "doc_items": ["3 Range item."]},
    ]
    values = _values(COPY, tasks)
    assert _step().call(None, values.__getitem__) == [
        "mapped watchdog doc items of the ledger copy: 3 items on 2 tasks, updated none"
    ]
    assert _writes(values) == []
    assert _coordination(values)[-2:] == [("mark", (doc_items.MARK, doc_items.digest(COPY))), _seen()]


@pytest.mark.feature("tick-doc-items-map")
def test_unknown_tasks_refuse_the_copy_once_until_it_changes() -> None:
    copy = COPY + "4. [open] Lost item. [Ledger: t9, t8]\n"
    values = _values(copy, _tasks())
    assert _step().call(None, values.__getitem__) == [
        "refused watchdog doc items of the ledger copy: Unknown ledger tasks: t8, t9"
    ]
    assert _writes(values) == []
    assert _coordination(values)[-2:] == [("mark", (doc_items.MARK, doc_items.digest(copy))), _seen()]


@pytest.mark.feature("tick-doc-items-map")
def test_an_invalid_ledger_tag_refuses_the_copy_once_until_it_changes() -> None:
    copy = COPY + "4. [open] Bad tag. [Ledger: nine]\n"
    values = _values(copy, _tasks())
    assert _step().call(None, values.__getitem__) == [
        "refused watchdog doc items of the ledger copy: Invalid Ledger tag:  nine"
    ]
    assert _writes(values) == []
    assert _coordination(values)[-2:] == [("mark", (doc_items.MARK, doc_items.digest(copy))), _seen()]


@pytest.mark.feature("tick-doc-items-map")
def test_the_step_stops_when_its_deadline_expires() -> None:
    values = _values(COPY, _tasks())
    values["deadline"] = Deadline.after(-1)
    with pytest.raises(TimeoutError, match="tool deadline expired"):
        _step().call(None, values.__getitem__)


@pytest.mark.feature("tick-doc-items-map")
def test_the_coordination_store_reads_back_the_mark_it_wrote() -> None:
    from scripts.tick.adapters.coordination import StoreCoordination

    class _Redis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def set(self, key: str, value: str) -> bool:
            self.values[key] = value
            return True

        def get(self, key: str) -> str | None:
            return self.values.get(key)

    store = SimpleNamespace(redis=_Redis(), key=lambda slug, *parts: ":".join(("swarm", slug, *parts)))
    port = StoreCoordination(store, "sw", "owner")  # pyright: ignore[reportArgumentType]
    deadline = Deadline.after(5)
    assert port.marked(doc_items.MARK, deadline=deadline) is None
    assert port.mark(doc_items.MARK, "abc", deadline=deadline) is True
    assert port.marked(doc_items.MARK, deadline=deadline) == "abc"
    assert store.redis.values == {"swarm:sw:doc-items-digest": "abc"}


HOOK_S = 0.4


def _no_result(ctx: Context) -> Result:
    return Result()


@dataclass
class _Timed:
    name: str
    group: str
    needs: frozenset[str] = frozenset()
    lane: Lane = Lane("critical")
    budget_s: float = 6 * HOOK_S
    action: Callable[[Context], Result] = field(default=_no_result)
    interval_s: float = 0.0
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""

    def run(self, ctx: Context) -> Result:
        return self.action(ctx)


class _View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


def _pause(ctx: Context) -> Result:
    sleep(HOOK_S / 8)
    return Result()


def _late_view_hook() -> bool:
    sleep(HOOK_S)
    return True


def _never_shut(step: object) -> bool:
    return False


@pytest.mark.feature("tick-doc-items-map")
def test_the_step_runs_when_the_wrapper_view_hook_answers_past_its_budget() -> None:
    real = _step()
    view = {step.name: step for step in registry.steps()}["tick view"]
    steps = (
        _Timed("ledger state", "observe", lane=engine.SLOW, action=_pause),
        _Timed("drained resume", "observe", lane=engine.SLOW, action=_pause),
        _Timed("tick view", view.group),
        _Timed(real.name, real.group, real.needs, real.lane, HOOK_S / 4),
    )
    gate = engine.SiteGate({view.group: _late_view_hook}, _never_shut, sites=("observe", view.group, real.group))
    later: list[tuple[engine.StepRecord, ...]] = []
    with pool.running(workers=2, critical_s=6 * HOOK_S, slow_s=6 * HOOK_S):
        records = engine.run_graph(steps, Context(_View(), Deadline.after(10.0)), finished=later.append, gate=gate)
    found = {record.name: record for record in (*records, *later[0])}
    assert (found[real.name].outcome, found[real.name].rested) == (engine.OK, False)
