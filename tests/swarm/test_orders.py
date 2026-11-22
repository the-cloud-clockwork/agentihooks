from typing import Any

import pytest

from scripts.swarm import orders

pytestmark = pytest.mark.unit

Row = dict[str, Any]
PHASES: list[Row] = [{"id": "p1"}, {"id": "p2"}]


def task(task_id: str, phase: str = "p1", **fields: Any) -> Row:
    return {"id": task_id, "phase": phase, "state": "open", "lane": "eng", **fields}


def order(at: int) -> Row:
    return {"at": at, "by": "operator"}


def doc(tasks: list[Row], phases: list[Row] = PHASES) -> Row:
    return {"tasks": tasks, "phases": phases}


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_ledger_with_no_order_mark_has_no_order_time() -> None:
    assert orders.stamps(doc([task("a"), task("b", depends_on=["a"])])) == {}
    assert orders.stamps({"tasks": [task("a")]}) == {}
    assert orders.stamps({}) == {}


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_task_carries_the_earlier_of_its_own_order_time_and_its_phases() -> None:
    phases = [{"id": "p1"}, {"id": "p2", "ordered": order(9)}]
    tasks = [
        task("own", ordered=order(7)),
        task("phase", phase="p2"),
        task("sooner", phase="p2", ordered=order(3)),
        task("later", phase="p2", ordered=order(12)),
        task("plain"),
    ]
    assert orders.stamps(doc(tasks, phases)) == {"own": 7, "phase": 9, "sooner": 3, "later": 9}


@pytest.mark.feature("ready-ordered-offer-first")
def test_the_work_an_ordered_task_waits_on_carries_its_order_time() -> None:
    tasks = [
        task("o", ordered=order(5), depends_on=["d1"], parked_on=["k"]),
        task("d1", depends_on=["d2", "gone"]),
        task("d2"),
        task("k"),
        task("plain"),
    ]
    assert orders.stamps(doc(tasks)) == {"o": 5, "d1": 5, "d2": 5, "k": 5}


@pytest.mark.feature("ready-ordered-offer-first")
def test_every_task_of_a_phase_an_ordered_phase_waits_on_carries_its_order_time() -> None:
    phases = [{"id": "p1"}, {"id": "p2", "depends_on": ["p1"], "ordered": order(4)}, {"id": "p3"}]
    tasks = [task("a"), task("b"), task("c", phase="p2"), task("other", phase="p3")]
    assert orders.stamps(doc(tasks, phases)) == {"a": 4, "b": 4, "c": 4}


@pytest.mark.feature("ready-ordered-offer-first")
def test_the_plan_task_that_enhances_ordered_work_and_what_it_waits_on_carry_the_order_time() -> None:
    tasks = [
        task("o", ordered=order(6)),
        task("thin", lane="plan", enhances="o", depends_on=["before"]),
        task("before"),
        task("elsewhere", lane="plan", enhances="plain"),
        task("plain"),
    ]
    assert orders.stamps(doc(tasks)) == {"o": 6, "thin": 6, "before": 6}


@pytest.mark.feature("ready-ordered-offer-first")
def test_the_lead_an_ordered_task_is_merged_into_and_what_it_waits_on_carry_the_order_time() -> None:
    tasks = [
        task("member", ordered=order(8), merged_into="lead"),
        task("lead", depends_on=["dep"]),
        task("dep"),
        task("plain"),
    ]
    assert orders.stamps(doc(tasks)) == {"member": 8, "lead": 8, "dep": 8}


@pytest.mark.feature("ready-ordered-offer-first")
def test_the_order_time_travels_through_every_link_of_the_work_it_reaches() -> None:
    tasks = [
        task("o", ordered=order(6), depends_on=["dep"]),
        task("dep", merged_into="lead"),
        task("thin", lane="plan", enhances="dep"),
        task("lead", parked_on=["parked"]),
        task("polish", lane="plan", enhances="lead"),
        task("parked"),
        task("plain"),
    ]
    assert orders.stamps(doc(tasks)) == {"o": 6, "dep": 6, "thin": 6, "lead": 6, "polish": 6, "parked": 6}


@pytest.mark.feature("ready-ordered-offer-first")
def test_the_earliest_order_wins_wherever_two_orders_meet() -> None:
    tasks = [
        task("late", ordered=order(5), depends_on=["shared"]),
        task("early", ordered=order(3), depends_on=["shared", "late"]),
        task("shared", depends_on=["deep"]),
        task("deep"),
        task("after", ordered=order(9), depends_on=["early"]),
    ]
    assert orders.stamps(doc(tasks)) == {"late": 3, "early": 3, "shared": 3, "deep": 3, "after": 9}
    assert orders.stamps(doc(tasks[::-1])) == {"late": 3, "early": 3, "shared": 3, "deep": 3, "after": 9}


@pytest.mark.feature("ready-ordered-offer-first")
def test_done_and_out_of_scope_work_carries_no_order_time_and_passes_none_on() -> None:
    tasks = [
        task("done", ordered=order(1), state="done", depends_on=["behind"]),
        task("scoped", ordered=order(1), out_of_scope=True, depends_on=["behind"]),
        task("behind"),
        task("o", ordered=order(2), depends_on=["finished", "dropped"]),
        task("finished", state="done", depends_on=["past"]),
        task("dropped", out_of_scope=True, depends_on=["past"]),
        task("past"),
        task("busy", ordered=order(3), state="claimed", depends_on=["wip"]),
        task("wip", state="pr"),
    ]
    assert orders.stamps(doc(tasks)) == {"o": 2, "busy": 3, "wip": 3}


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_done_or_out_of_scope_phase_passes_its_order_time_to_no_task() -> None:
    phases = [{"id": "p1", "ordered": order(4), "done": True}, {"id": "p2", "ordered": order(5), "out_of_scope": True}]
    tasks = [task("a"), task("b", phase="p2"), task("c", ordered=order(7), done=True), task("d", ordered=order(8))]
    assert orders.stamps(doc(tasks, phases)) == {"d": 8}
    assert [orders.closed(row) for row in (*phases, tasks[2], tasks[0])] == [True, True, True, False]
    assert orders.closed({"state": "done"}) and not orders.closed({"state": "pr", "done": False})


@pytest.mark.feature("ready-ordered-offer-first")
def test_tasks_that_wait_on_each_other_end_with_the_earliest_order_time() -> None:
    tasks = [task("a", ordered=order(4), depends_on=["b"]), task("b", ordered=order(2), depends_on=["a"])]
    assert orders.stamps(doc(tasks)) == {"a": 2, "b": 2}
    tasks = [task("a", ordered=order(4), depends_on=["b"]), task("b", depends_on=["c"]), task("c", depends_on=["a"])]
    assert orders.stamps(doc(tasks)) == {"a": 4, "b": 4, "c": 4}


@pytest.mark.parametrize("mark", ["yes", 7, None, [], {}, {"by": "operator"}, {"at": "7"}, {"at": 7.5}, {"at": True}])
@pytest.mark.feature("ready-ordered-offer-first")
def test_a_malformed_order_mark_counts_as_no_mark(mark: object) -> None:
    phases = [{"id": "p1", "ordered": mark}, {"id": "p2"}]
    tasks = [task("a", ordered=mark, depends_on=["b"]), task("b"), task("c")]
    assert orders.stamps(doc(tasks, phases)) == {}
    assert orders.marked({"ordered": mark}) is None


@pytest.mark.feature("ready-ordered-offer-first")
def test_a_well_formed_order_mark_gives_its_time() -> None:
    assert orders.marked({"ordered": {"at": 0}}) == 0
    assert (
        orders.marked({"ordered": {"at": 1795300000000, "by": "master@c0ffee-0001", "quote": "Build it"}})
        == 1795300000000
    )
    assert orders.marked({}) is None
    assert orders.stamps(doc([task("a", ordered={"at": 0})])) == {"a": 0}
