"""The engine applies the writes steps return through compare and set appliers and records the ones it drops."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import difficulty, grouping
from scripts.swarm.ledger_client import LedgerRefused
from scripts.tick import engine, pool, view
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Lane, Result, View, Write
from scripts.tick.steps import actions, actions_of, budgets, tasks

pytestmark = pytest.mark.unit

TASKS = Domain("tasks")


@dataclass
class Store:
    rows: dict[str, tuple[int, object]] = field(default_factory=lambda: {"t1": (1, "unsized")})
    applied: list[Write] = field(default_factory=list)

    def read(self, domain: Domain, key: str) -> object | None:
        return self.rows.get(key)

    def apply(self, write: Write) -> None:
        revision, _ = self.rows[write.key]
        if revision != write.expected:
            raise RevisionConflict(f"tasks/{write.key} moved from revision {write.expected} to {revision}")
        self.rows[write.key] = (revision + 1, write.value)
        self.applied.append(write)


@dataclass
class SizeStep:
    store: Store
    rival: list[object] = field(default_factory=list)
    name: str = "size"
    group: str = "tasks"
    lane: Lane = Lane("main")
    budget_s: float = 5.0
    interval_s: float = 0.0
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset({TASKS})
    writes: frozenset[Domain] = frozenset({TASKS})
    effects: frozenset[str] = frozenset()
    feature: str = ""

    def run(self, ctx: Context) -> Result:
        found = ctx.view.read(TASKS, "t1")
        assert isinstance(found, tuple)
        revision = found[0]
        for value in self.rival:
            self.store.rows["t1"] = (revision + 1, value)
        self.rival.clear()
        return Result(
            (
                Write(TASKS, "t1", f"M from revision {revision}", expected=revision),
                Write(actions_of(self.name), self.name, [f"sized from revision {revision}"]),
            )
        )


def _pass(store: Store, step: SizeStep) -> tuple[engine.StepRecord, ...]:
    return engine.run_group("tasks", Context(store, Deadline.after(60.0)), [step], apply={TASKS: store.apply})


def test_a_conflicting_write_is_dropped_and_recomputed_on_the_next_pass() -> None:
    store = Store()
    step = SizeStep(store, rival=["L by a concurrent step"])

    (first,) = _pass(store, step)

    dropped = Write(TASKS, "t1", "M from revision 1", expected=1)
    assert first.dropped == (dropped,)
    assert store.applied == []
    assert store.rows == {"t1": (2, "L by a concurrent step")}
    assert actions([first]) == [
        "sized from revision 1",
        "dropped write tasks t1 of step size: tasks/t1 moved from revision 1 to 2; the next pass recomputes it",
    ]

    (second,) = _pass(store, step)

    assert second.dropped == ()
    assert store.applied == [Write(TASKS, "t1", "M from revision 2", expected=2)]
    assert store.rows == {"t1": (3, "M from revision 2")}
    assert actions([second]) == ["sized from revision 2"]


def test_a_parallel_pass_drops_the_conflicting_write_and_recomputes_it() -> None:
    store = Store()
    step = SizeStep(store, rival=["L by a concurrent step"])

    with pool.running(workers=2):
        (first,) = _pass(store, step)
        (second,) = _pass(store, step)

    assert first.dropped == (Write(TASKS, "t1", "M from revision 1", expected=1),)
    assert second.dropped == ()
    assert store.rows == {"t1": (3, "M from revision 2")}


def test_writes_without_an_applier_pass_through_untouched() -> None:
    store = Store()
    step = SizeStep(store)

    (record,) = engine.run_group("tasks", Context(store, Deadline.after(60.0)), [step])

    assert record.dropped == ()
    assert store.applied == []
    assert store.rows == {"t1": (1, "unsized")}
    assert record.result is not None
    assert len(record.result.writes) == 2


def test_a_write_defaults_to_no_expected_revision() -> None:
    assert Write(TASKS, "t1", "S").expected is None


class Ledger:
    def __init__(self, refuse: bool = False, held: dict[str, dict[str, object]] | None = None) -> None:
        self.refuse = refuse
        self.held = held if held is not None else {}
        self.updates: list[tuple[str, str, object]] = []
        self.guards: list[dict[str, object] | None] = []

    def update_task(
        self, slug: str, task_id: str, fields: dict[str, object], if_match: dict[str, object] | None = None
    ) -> dict[str, object]:
        if self.refuse:
            raise LedgerRefused("refused")
        self.guards.append(if_match)
        task = self.held.setdefault(task_id, {"id": task_id})
        if any(task.get(key, "") != value for key, value in (if_match or {}).items()):
            return dict(task)
        task.update(fields)
        self.updates.append((slug, task_id, fields))
        return dict(task)


def test_the_tasks_applier_writes_while_the_expected_fields_hold() -> None:
    ledger = Ledger()
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1"}}.get)

    apply(Write(TASKS, "t1", {"difficulty": "M"}, expected={"difficulty": ""}))

    assert ledger.updates == [("sw", "t1", {"difficulty": "M"})]


def test_the_tasks_applier_drops_a_write_whose_expected_field_moved() -> None:
    ledger = Ledger()
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1", "difficulty": "L"}}.get)

    with pytest.raises(RevisionConflict, match="^task t1 changed difficulty since the step read it$"):
        apply(Write(TASKS, "t1", {"difficulty": "M"}, expected={"difficulty": ""}))
    assert ledger.updates == []


def test_the_tasks_applier_turns_a_refused_write_into_a_conflict() -> None:
    apply = tasks.task_writer("sw", Ledger(refuse=True), {}.get)

    with pytest.raises(RevisionConflict, match="^the ledger refused the write of task t1$"):
        apply(Write(TASKS, "t1", {"difficulty": "M"}))


def test_appliers_need_a_ledger_and_a_swarm() -> None:
    assert tasks.appliers({"slug": "sw"}) == {}
    assert tasks.appliers({"ledger": Ledger()}) == {}
    assert list(tasks.appliers({"slug": "sw", "ledger": Ledger()})) == [tasks.SIZES, tasks.GROUPS]


def test_the_size_pass_returns_its_sizes_as_guarded_task_writes() -> None:
    fields = {"difficulty": "S", "difficulty_source": "rule"}
    tick = SimpleNamespace(
        difficulty=SimpleNamespace(
            sizes=lambda doc: [("t1", fields)] if doc is marker else [], sized_action=difficulty.sized_action
        )
    )
    marker: dict[str, object] = {"tasks": []}

    assert tasks.size_writes(tick, marker) == [
        Write(tasks.SIZES, "t1", fields, expected={"difficulty": ""}),
        "sized task t1 S by rule",
    ]


def test_the_size_step_splits_actions_from_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    fields = {"difficulty": "S", "difficulty_source": "rule"}
    tick = SimpleNamespace(
        skip_refused=lambda function, *args: function(*args),
        difficulty=SimpleNamespace(sizes=lambda doc: [("t1", fields)], sized_action=difficulty.sized_action),
    )
    monkeypatch.setattr("scripts.tick.steps.import_module", lambda name: tick)
    (step,) = [step for step in tasks.STEPS if step.name == "size pass"]
    state: dict[str, object] = {"doc": "doc", "box": {}}

    found = step.run(
        Context(cast(View, SimpleNamespace(read=lambda domain, key: state.get(key))), Deadline.after(60.0))
    )

    assert found.writes == (
        Write(actions_of("size pass"), "size pass", ["sized task t1 S by rule"]),
        Write(tasks.SIZES, "t1", fields, expected={"difficulty": ""}),
    )
    assert tasks.SIZES in step.writes
    assert TASKS not in step.writes


def test_a_write_step_keeps_every_attribute_of_its_call_step() -> None:
    def call(tick: object, get: object) -> list[object]:
        return []

    step = tasks.WriteStep("size pass", "sizing", call, writes=frozenset({tasks.SIZES}))

    assert step.name == "size pass"
    assert step.group == "sizing"
    assert step.call is call
    assert step.lane == Lane("tick")
    assert (step.budget_s, step.interval_s) == budgets.timing_of("size pass")
    assert step.budget_s is not None
    assert step.needs == frozenset()
    assert step.reads == frozenset()
    assert step.writes == frozenset({tasks.SIZES, actions_of("size pass")})


class GroupLedger:
    def __init__(self, refuse: bool = False, members: dict[str, list[str]] | None = None) -> None:
        self.refuse = refuse
        self.members = members if members is not None else {}
        self.ungrouped: list[tuple[str, str]] = []
        self.guards: list[dict[str, list[str]] | None] = []

    def ungroup_tasks(self, slug: str, lead: str, if_match: dict[str, list[str]] | None = None) -> None:
        self.guards.append(if_match)
        held = self.members.get(lead)
        if self.refuse or (if_match is not None and held is not None and held != if_match["group_members"]):
            raise LedgerRefused("refused")
        self.ungrouped.append((slug, lead))


def test_the_groups_applier_releases_while_the_lead_holds_the_members_it_read() -> None:
    ledger = GroupLedger()
    apply = tasks.group_writer("sw", ledger, {"l1": {"id": "l1", "group_members": ["m1", "m2"]}}.get)

    apply(Write(tasks.GROUPS, "l1", ["m1"], expected={"group_members": ["m1", "m2"]}))

    assert ledger.ungrouped == [("sw", "l1")]


def test_the_groups_applier_drops_a_release_whose_members_moved() -> None:
    ledger = GroupLedger()
    apply = tasks.group_writer("sw", ledger, {"l1": {"id": "l1", "group_members": ["m1"]}}.get)

    with pytest.raises(RevisionConflict, match="^task l1 changed group_members since the step read it$"):
        apply(Write(tasks.GROUPS, "l1", ["m1"], expected={"group_members": ["m1", "m2"]}))
    assert ledger.ungrouped == []


def test_the_groups_applier_turns_a_refused_release_into_a_conflict() -> None:
    apply = tasks.group_writer("sw", GroupLedger(refuse=True), {"l1": {"group_members": ["m1"]}}.get)

    with pytest.raises(RevisionConflict, match="^the ledger refused the release of the group under task l1$"):
        apply(Write(tasks.GROUPS, "l1", ["m1"], expected={"group_members": ["m1"]}))


def test_the_grouping_release_returns_guarded_group_writes_and_leaves_the_document() -> None:
    doc = {
        "tasks": [
            {"id": "l1", "state": "done", "group_members": ["m1", "m2"]},
            {"id": "m1", "state": "open", "merged_into": "l1"},
            {"id": "m2", "state": "open", "merged_into": "other"},
        ]
    }
    before = repr(doc)
    tick = SimpleNamespace(grouping=grouping)

    assert tasks.release_writes(tick, "sw", None, doc) == [
        Write(tasks.GROUPS, "l1", ["m1"], expected={"group_members": ["m1", "m2"]}),
        f"released tasks m1 from task l1: its lead {grouping.RELEASE_CLOSED}",
    ]
    assert repr(doc) == before


def test_the_fresh_task_reads_the_held_view_and_is_none_without_one() -> None:
    with view.holding():
        assert tasks._fresh_task("t1") is None
        view.CURRENT.set(view.LedgerView(1, {"tasks": [{"id": "t1", "difficulty": "L"}], "phases": [{"id": "t1"}]}))
        assert tasks._fresh_task("t1") == {"id": "t1", "difficulty": "L"}
        assert tasks._fresh_task("t2") == {}


def test_the_tasks_applier_names_every_moved_field_in_order() -> None:
    ledger = Ledger()
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1", "difficulty": "L", "size": "big"}}.get)

    with pytest.raises(RevisionConflict, match="^task t1 changed difficulty, size since the step read it$"):
        apply(Write(TASKS, "t1", {"difficulty": "M"}, expected={"size": "", "difficulty": ""}))
    assert ledger.updates == []


def test_the_groups_applier_reads_no_expected_members_as_none() -> None:
    ledger = GroupLedger()
    apply = tasks.group_writer("sw", ledger, {"l1": {"id": "l1", "group_members": []}}.get)

    apply(Write(tasks.GROUPS, "l1", []))

    assert ledger.ungrouped == [("sw", "l1")]


def test_the_tasks_applier_compares_and_sets_its_expected_fields_on_the_server() -> None:
    ledger = Ledger()
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1"}}.get)

    apply(Write(TASKS, "t1", {"difficulty": "M"}, expected={"difficulty": ""}))

    assert ledger.guards == [{"difficulty": ""}]
    assert ledger.held["t1"]["difficulty"] == "M"


def test_the_tasks_applier_sends_no_guard_for_a_write_without_expected_fields() -> None:
    ledger = Ledger()
    apply = tasks.task_writer("sw", ledger, {}.get)

    apply(Write(TASKS, "t1", {"difficulty": "M"}))

    assert ledger.guards == [None]


def test_the_tasks_applier_drops_a_size_a_concurrent_write_set_after_the_step_read() -> None:
    ledger = Ledger(held={"t1": {"id": "t1", "difficulty": "L"}})
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1"}}.get)

    with pytest.raises(RevisionConflict) as refused:
        apply(Write(TASKS, "t1", {"difficulty": "M", "difficulty_source": "rule"}, expected={"difficulty": ""}))
    assert str(refused.value) == "task t1 changed difficulty on the ledger before the write"
    assert ledger.held["t1"] == {"id": "t1", "difficulty": "L"}
    assert ledger.updates == []


def test_the_tasks_applier_names_every_field_a_concurrent_write_set_after_the_step_read() -> None:
    ledger = Ledger(held={"t1": {"id": "t1", "difficulty": "L", "size": "XL"}})
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1"}}.get)

    with pytest.raises(RevisionConflict) as refused:
        apply(Write(TASKS, "t1", {"size": "S", "difficulty": "M"}, expected={"size": "", "difficulty": ""}))
    assert str(refused.value) == "task t1 changed difficulty, size on the ledger before the write"
    assert ledger.updates == []


def test_the_tasks_applier_reads_a_field_the_ledger_left_out_as_empty() -> None:
    ledger = SimpleNamespace(update_task=lambda *args, **kwargs: {})
    apply = tasks.task_writer("sw", ledger, {"t1": {"id": "t1"}}.get)

    apply(Write(TASKS, "t1", {"difficulty": ""}, expected={"difficulty": ""}))


def test_the_groups_applier_compares_and_sets_the_members_on_the_server() -> None:
    ledger = GroupLedger(members={"l1": ["m1", "m2"]})
    apply = tasks.group_writer("sw", ledger, {"l1": {"id": "l1", "group_members": ["m1", "m2"]}}.get)

    apply(Write(tasks.GROUPS, "l1", ["m1"], expected={"group_members": ["m1", "m2"]}))

    assert ledger.guards == [{"group_members": ["m1", "m2"]}]
    assert ledger.ungrouped == [("sw", "l1")]


def test_the_groups_applier_drops_a_release_a_concurrent_regroup_beat_on_the_server() -> None:
    ledger = GroupLedger(members={"l1": ["m1", "m3"]})
    apply = tasks.group_writer("sw", ledger, {"l1": {"id": "l1", "group_members": ["m1", "m2"]}}.get)

    with pytest.raises(RevisionConflict) as refused:
        apply(Write(tasks.GROUPS, "l1", ["m1"], expected={"group_members": ["m1", "m2"]}))
    assert str(refused.value) == "the ledger refused the release of the group under task l1"
    assert ledger.ungrouped == []


def test_the_groups_applier_guards_a_release_without_expected_members_on_no_members() -> None:
    ledger = GroupLedger(members={"l1": ["m1"]})
    apply = tasks.group_writer("sw", ledger, {}.get)

    with pytest.raises(RevisionConflict) as refused:
        apply(Write(tasks.GROUPS, "l1", []))
    assert str(refused.value) == "the ledger refused the release of the group under task l1"

    assert ledger.guards == [{"group_members": []}]
    assert ledger.ungrouped == []


def test_the_grouping_release_returns_a_write_for_every_stopped_lead() -> None:
    doc = {
        "tasks": [
            {"id": "l1", "state": "done", "group_members": ["m1"]},
            {"id": "m1", "state": "open", "merged_into": "l1"},
            {"id": "l2", "state": "done", "group_members": ["m2"]},
            {"id": "m2", "state": "open", "merged_into": "l2"},
        ]
    }

    found = tasks.release_writes(SimpleNamespace(grouping=grouping), "sw", None, doc)

    assert [item.key for item in found if isinstance(item, Write)] == ["l1", "l2"]
    assert len(found) == 4


def test_dropped_writes_and_their_conflicts_must_pair_up() -> None:
    write = Write(TASKS, "t1", "M")
    record = engine.StepRecord("size", 0.0, 1.0, result=Result(()), dropped=(write, write), conflicts=("moved",))

    with pytest.raises(ValueError, match="zip"):
        actions([record])


class ShortPool:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self) -> "ShortPool":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def map(self, function: object, items: list[object]) -> list[object]:
        return []


def test_the_size_pass_refuses_a_pool_that_loses_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(difficulty, "ThreadPoolExecutor", ShortPool)
    doc = {"tasks": [{"id": "t1", "state": "open", "territory": [], "kind": "ops"}]}

    with pytest.raises(ValueError, match="zip"):
        list(difficulty.sizes(doc))
