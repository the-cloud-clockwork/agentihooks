"""The thin task pass tick step queues one enhancement plan task for each thin open engineer or ci task, marks the
task as needing enhancement in the same ledger write and tells the master once."""

import io
import json
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from email.message import Message
from importlib import import_module
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm import thin_task
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm_ledger import ledger_enhancement, ledger_server
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import schemas
from scripts.swarm_ledger.api.errors import APIError
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeLedger, fake_tools
from scripts.tick.adapters.ledger import ClientLedger
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep, actions_of, hook_domains, thin
from tests.swarm.generated_release import generated_release

pytestmark = pytest.mark.unit

SLUG = "sw"
EFFECTS = frozenset({"inbox", "ledger"})
NEEDED = {"enhancement": "needed"}
MARK = {
    "op": "task_enhancement",
    "id": "thin-mark-t1",
    "by": "swarm",
    "item": "tasks/t1",
    "enhancement": "needed",
    "expected": None,
}
THIN: dict[str, Any] = {
    "id": "t1",
    "title": "Fix the claim",
    "state": "open",
    "lane": "eng",
    "kind": "code",
    "phase": "p1",
    "description": "Fix the claim.",
}
PHASELESS = {key: value for key, value in THIN.items() if key != "phase"}
OWN_PHASE = {
    "op": "phase_add",
    "id": "thin-phase-t1",
    "by": "swarm",
    "phase": "enhance-t1",
    "title": "Plan the thin task Fix the claim",
    "description": "Holds task t1 and its enhancement plan, so the planner publishes that plan into this phase.",
    "planning": "manual",
    "not_duplicate": "Each thin task with no phase gets a phase of its own for its enhancement plan.",
}
MOVE = {
    "op": "task_update",
    "id": "thin-move-t1",
    "by": "swarm",
    "item": "tasks/t1",
    "fields": {"phase": "enhance-t1"},
}
FULL = (
    "Add the step body seam scripts.tick.steps.thin so every open engineer task with a thin text gets one planner "
    "enhancement task and the master hears about it once, while tasks that already carry a plan stay untouched by "
    "the pass. Tests observe scripts.tick.steps.thin writes one planner enhancement per thin task. "
    "Done when the pull request is merged."
)


@dataclass
class Inbox:
    sent: list[tuple[str, str, str, bool]] = field(default_factory=list[tuple[str, str, str, bool]])

    def send(self, sender: str, address: str, text: str, fyi: bool = False) -> None:
        self.sent.append((sender, address, text, fyi))


@dataclass
class RefusingLedger(FakeLedger):
    refused: str = ""

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object:
        if any(operation.get("enhances") == self.refused for operation in operations):
            raise LedgerRefused("task_add refused")
        return super().mutate(operations, deadline=deadline)


@dataclass
class Accepting:
    """A ledger API client that grades every batch it is sent with the ledger API's own operation check."""

    sent: list[list[dict[str, Any]]] = field(default_factory=list[list[dict[str, Any]]])
    refused: list[str] = field(default_factory=list[str])

    def mutate(self, slug: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        self.sent.append(operations)
        try:
            schemas.check_operations(
                {"operation_id": slug, "ops": operations, "guards": {}}, ledger_server.core, ("t1",)
            )
        except APIError as error:
            self.refused.append(str(error))
            return {"rejected": ["refused"]}
        return {}


@dataclass
class Pass:
    lines: list[str]
    ledger: FakeLedger
    inbox: Inbox

    def calls(self) -> list[tuple[str, tuple[object, ...]]]:
        return [(call.operation, call.args) for call in self.ledger.calls]


def _step() -> CallStep:
    import_module("scripts.tick.steps.thin")
    found = {step.name: step for step in registry.steps()}["thin task pass"]
    assert isinstance(found, CallStep)
    return found


def _run(
    tasks: list[dict[str, Any]],
    ledger: FakeLedger | None = None,
    deadline: Deadline | None = None,
    doc: dict[str, Any] | None = None,
) -> Pass:
    held = ledger or FakeLedger({})
    inbox = Inbox()
    values: dict[str, Any] = {
        "doc": {**(doc or {}), "tasks": tasks},
        "tools": replace(fake_tools(), ledger=held).for_effects(EFFECTS),
        "deadline": deadline or Deadline.after(5),
        "slug": SLUG,
        "inbox": inbox,
    }
    call: Callable[[Any, Callable[[str], Any]], Any] = _step().call
    return Pass(call(None, values.__getitem__), held, inbox)


def _operations(found: Pass) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for name, args in found.calls():
        if name == "mutate":
            operations += [dict(operation) for operation in cast(Sequence[Mapping[str, Any]], args[0])]
    return operations


def _added(found: Pass) -> list[dict[str, Any]]:
    return [operation for operation in _operations(found) if operation["op"] == "task_add"]


def _applied(tasks: list[dict[str, Any]], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ledger rows after a pass's writes: each task add as a new open row in its lane and phase, each task update
    merged into its target and each enhancement operation set on its target; a phase add leaves the rows as they are."""
    rows = {task["id"]: dict(task) for task in tasks}
    for operation in operations:
        if operation["op"] == "task_add":
            added = {"id": operation["task"], "state": "open", "lane": operation["lane"], "phase": operation["phase"]}
            rows[operation["task"]] = added
        elif operation["op"] == "task_update":
            rows[operation["item"].removeprefix("tasks/")].update(operation["fields"])
        elif operation["op"] == "task_enhancement":
            rows[operation["item"].removeprefix("tasks/")]["enhancement"] = operation["enhancement"]
    return list(rows.values())


def _notice(names: str) -> str:
    return (
        f"For your information: the thin task check queued {names} for a planner; "
        "each enhanced task is held from engineer and ci claims until its plan closes."
    )


@pytest.mark.feature("tick-thin-task-pass")
def test_thin_task_pass() -> None:
    """The thin task pass step registers in group wrapper phases on the tick lane every 120 seconds, reading the
    ledger doc and writing through the ledger and inbox ports."""
    found = _step()
    assert (found.group, found.lane, found.interval_s, found.floor_s, found.feature, found.effects) == (
        "wrapper phases",
        "tick",
        120.0,
        2.0,
        "tick-thin-task-pass",
        EFFECTS,
    )
    assert (found.needs, found.reads, found.writes) == (
        frozenset(),
        hook_domains("doc"),
        {actions_of("thin task pass")},
    )


@pytest.mark.feature("tick-thin-task-pass")
def test_a_thin_open_task_gets_one_enhancement_task_on_the_first_pass_and_none_on_the_next() -> None:
    """The first pass adds enhance-t1 in the target's phase and marks t1 as needing enhancement in one ledger write and
    tells the master once; the next pass over the ledger that write produced changes nothing."""
    first = _run([THIN])
    [added, marked] = _operations(first)
    assert {key: added[key] for key in ("op", "id", "by", "task", "title", "lane", "kind", "phase", "enhances")} == {
        "op": "task_add",
        "id": "thin-add-t1",
        "by": "swarm",
        "task": "enhance-t1",
        "title": "Plan the thin task Fix the claim",
        "lane": "plan",
        "kind": "plan",
        "phase": "p1",
        "enhances": "t1",
    }
    assert added["description"] == (
        "Write a full plan section for task t1 before an engineer claims it. It fails these rules: "
        f"{' '.join(thin_task.thin(THIN))} Its current description: Fix the claim."
    )
    assert added["not_duplicate"] == "Each thin task gets its own enhancement plan task, keyed by the task it enhances."
    assert marked == MARK
    assert [name for name, _ in first.calls()] == ["mutate"]
    assert first.lines == ["queued enhance-t1 for thin task t1"]
    assert first.inbox.sent == [("swarm", f"master@{SLUG}", _notice("enhance-t1"), True)]
    after = _applied([THIN], _operations(first))
    assert after == [{**THIN, **NEEDED}, {"id": "enhance-t1", "state": "open", "lane": "plan", "phase": "p1"}]
    second = _run(after)
    assert (second.lines, second.calls(), second.inbox.sent) == ([], [], [])


@pytest.mark.feature("tick-thin-task-pass")
@pytest.mark.parametrize(
    "task",
    [
        {**THIN, "description": FULL, "territory": ["scripts/tick/steps"]},
        {**THIN, "state": "claimed"},
        {**THIN, "state": "done"},
        {**THIN, "lane": "plan", "kind": "plan"},
        {**THIN, **NEEDED},
        {**THIN, "enhancement": "planned"},
        {**THIN, "enhancement": "skipped"},
        {**THIN, "plan_slice": "thin-tick-step"},
        {**THIN, "out_of_scope": True},
    ],
    ids=["passing", "claimed", "done", "plan lane", "needed", "planned", "skipped", "sliced", "out of scope"],
)
def test_a_task_that_is_not_an_unmarked_thin_open_engineer_task_gets_no_write(task: dict[str, Any]) -> None:
    """Passing, claimed, closed, plan lane, already marked, sliced and out of scope tasks get no ledger write and no
    master item."""
    found = _run([task])
    assert (found.lines, found.calls(), found.inbox.sent) == ([], [], [])


@pytest.mark.feature("thin-release-task-exempt")
def test_the_generated_release_task_gets_no_write_on_repeated_passes() -> None:
    """The release task the phase builder generates gets no enhancement task, no mark and no master notice, on the
    first pass and on every pass after it."""
    release = generated_release()
    for _ in range(2):
        found = _run([release])
        assert (found.lines, found.calls(), found.inbox.sent) == ([], [], [])


@pytest.mark.feature("thin-release-task-exempt")
def test_an_ordinary_thin_ops_task_beside_the_release_task_gets_exactly_one_enhancement() -> None:
    """An ordinary thin ops task in the same pass as the generated release task gets one enhancement task and one mark
    and is the only task the master hears about; the next pass over the rows that write produced changes nothing."""
    release, ops = generated_release(), {**THIN, "kind": "ops"}
    first = _run([release, ops])
    assert [(added["task"], added["enhances"]) for added in _added(first)] == [("enhance-t1", "t1")]
    assert [operation for operation in _operations(first) if operation["op"] == "task_enhancement"] == [MARK]
    assert first.lines == ["queued enhance-t1 for thin task t1"]
    assert first.inbox.sent == [("swarm", f"master@{SLUG}", _notice("enhance-t1"), True)]
    after = _applied([release, ops], _operations(first))
    assert after[0] == release
    second = _run(after)
    assert (second.lines, second.calls(), second.inbox.sent) == ([], [], [])


@pytest.mark.feature("thin-skip-open-pr")
@pytest.mark.parametrize(
    ("fields", "checked"),
    [
        ({"pr_url": "https://github.com/o/r/pull/7"}, False),
        ({"branch": "engineer-1"}, False),
        ({"pr_url": "", "branch": ""}, True),
    ],
    ids=["pull request", "branch", "empty"],
)
def test_only_an_open_task_without_a_pull_request_or_branch_gets_the_thin_mark(
    fields: dict[str, Any], checked: bool
) -> None:
    """A thin open task that carries a pull request link or a branch gets no enhancement write, while empty values
    still leave it checked and marked."""
    held = {**THIN, **fields}
    found = _run([held])
    assert (thin.unchecked(held), bool(_operations(found)), bool(found.lines)) == (checked, checked, checked)


@pytest.mark.feature("tick-thin-task-pass")
@pytest.mark.parametrize("lane", [{"lane": "ci", "kind": "ci"}, {"lane": "eng"}, {}], ids=["ci", "eng", "no lane"])
def test_a_thin_ci_or_engineer_task_is_queued_whatever_its_lane_field(lane: dict[str, str]) -> None:
    """A thin ci task, a thin engineer task and a thin task with no lane field are each queued for enhancement."""
    task = {key: value for key, value in THIN.items() if key != "lane"} | lane
    found = _run([task])
    assert [added["task"] for added in _added(found)] == ["enhance-t1"]


@pytest.mark.feature("tick-thin-task-pass")
def test_an_existing_enhancement_task_skips_the_add_and_still_marks_the_task() -> None:
    """When the ledger already holds enhance-t1 the pass adds nothing and its one write only marks t1."""
    found = _run([THIN, {"id": "enhance-t1", "state": "open", "lane": "plan", "kind": "plan", "enhances": "t1"}])
    assert [name for name, _ in found.calls()] == ["mutate"]
    assert _operations(found) == [MARK]
    assert found.lines == ["queued enhance-t1 for thin task t1"]


@pytest.mark.feature("tick-thin-task-pass")
def test_a_task_with_no_description_field_is_checked_as_an_empty_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ledger row without a description field reaches the thin check with an empty description, as the ledger
    stores a task added without one, so the pass never fails on such a row."""
    checked: list[dict[str, Any]] = []
    monkeypatch.setattr(thin_task, "thin", lambda task: checked.append(task) or [])
    task = {key: value for key, value in THIN.items() if key != "description"}
    found = _run([task])
    assert checked == [{"description": "", **task}]
    assert (found.lines, found.calls()) == ([], [])


@pytest.mark.feature("tick-thin-task-pass")
def test_a_refused_write_is_reported_and_the_pass_goes_on_to_the_next_task() -> None:
    """A ledger refusal on t1 leaves t1 unmarked, and the pass still queues t2 and tells the master only about t2."""
    ledger = RefusingLedger({}, refused="t1")
    found = _run([THIN, {**THIN, "id": "t2"}], ledger)
    assert found.lines == ["refused the enhancement of t1: task_add refused", "queued enhance-t2 for thin task t2"]
    assert [operation["id"] for operation in _operations(found)] == ["thin-add-t2", "thin-mark-t2"]
    assert found.inbox.sent == [("swarm", f"master@{SLUG}", _notice("enhance-t2"), True)]


@pytest.mark.feature("tick-thin-task-pass")
def test_a_title_the_ledger_would_refuse_falls_back_to_a_plain_title() -> None:
    """A target title over the item word limit gives the enhancement task the plain fallback title."""
    found = _run([{**THIN, "title": "word " * 41}])
    [added] = _added(found)
    assert added["title"] == "Plan a thin task before an engineer claims it"


@pytest.mark.feature("tick-thin-task-pass")
@pytest.mark.parametrize("title", [{}, {"title": ""}], ids=["no title field", "empty title"])
def test_a_task_with_no_title_gets_the_plain_title(title: dict[str, str]) -> None:
    """A target row with no title or an empty one gives the enhancement task the plain title, so the pass never
    fails on such a row."""
    task = {key: value for key, value in THIN.items() if key != "title"} | title
    [added] = _added(_run([task]))
    assert added["title"] == "Plan a thin task before an engineer claims it"


@pytest.mark.feature("tick-thin-task-pass")
def test_two_thin_tasks_are_both_queued_and_named_in_one_master_notice() -> None:
    """Two thin tasks queued in one pass reach the master as a single notice naming both enhancement tasks."""
    found = _run([THIN, {**THIN, "id": "t2"}])
    assert found.lines == ["queued enhance-t1 for thin task t1", "queued enhance-t2 for thin task t2"]
    assert found.inbox.sent == [("swarm", f"master@{SLUG}", _notice("enhance-t1, enhance-t2"), True)]


@pytest.mark.feature("tick-thin-task-pass")
def test_a_thin_task_with_no_phase_gets_a_manual_phase_of_its_own_holding_it_and_its_enhancement() -> None:
    """A thin task with no phase gets, in its one ledger write, a manual planning phase named like its enhancement
    task, a move into that phase, its enhancement task added in it and its mark, in that order; the description of a
    task with none says so, and the next pass over the rows that write produced changes nothing."""
    task = PHASELESS | {"description": ""}
    first = _run([task])
    [phase, moved, added, marked] = _operations(first)
    assert (phase, moved, marked) == (OWN_PHASE, MOVE, MARK)
    assert {key: added[key] for key in ("op", "id", "task", "lane", "kind", "phase", "enhances")} == {
        "op": "task_add",
        "id": "thin-add-t1",
        "task": "enhance-t1",
        "lane": "plan",
        "kind": "plan",
        "phase": "enhance-t1",
        "enhances": "t1",
    }
    assert added["description"].endswith("Its current description: none.")
    assert [name for name, _ in first.calls()] == ["mutate"]
    assert first.lines == ["queued enhance-t1 for thin task t1"]
    after = _applied([task], _operations(first))
    assert after == [
        {**task, "phase": "enhance-t1", **NEEDED},
        {"id": "enhance-t1", "state": "open", "lane": "plan", "phase": "enhance-t1"},
    ]
    second = _run(after)
    assert (second.lines, second.calls(), second.inbox.sent) == ([], [], [])


@pytest.mark.feature("tick-thin-task-pass")
def test_a_spent_deadline_takes_no_task() -> None:
    """A pass whose deadline is already spent takes no task and writes nothing."""
    found = _run([THIN], deadline=Deadline(0.0, clock=lambda: 1.0))
    assert (found.lines, found.calls(), found.inbox.sent) == ([], [], [])


def _frozen(target: str) -> dict[str, Any]:
    """A ledger with phase p1 under plan a, phase p2 under plan b and one freeze record on ``target``."""
    return {
        "plans": [{"id": "a", "title": "Claims"}, {"id": "b", "title": "Hives"}],
        "phases": [{"id": "p1", "title": "Build", "plan": "plans/a"}, {"id": "p2", "title": "Ship", "plan": "plans/b"}],
        "freezes": [
            {"id": f"freeze-{target}", "verb": "freeze", "target": target, "by": "operator", "at": 1, "reason": ""}
        ],
    }


@pytest.mark.feature("tick-thin-task-pass")
@pytest.mark.parametrize(
    "target", ["tasks/t1", "phases/p1", "plans/a"], ids=["task freeze", "phase freeze", "plan freeze"]
)
def test_a_thin_task_held_by_a_freeze_gets_no_enhancement_write(target: str) -> None:
    """A thin open task under a freeze on itself, its phase or its phase's plan gets no ledger write, no action line
    and no master notice."""
    found = _run([THIN], doc=_frozen(target))
    assert (found.lines, found.calls(), found.inbox.sent) == ([], [], [])


@pytest.mark.feature("tick-thin-task-pass")
def test_an_unfrozen_thin_task_beside_a_frozen_phase_still_gets_its_enhancement() -> None:
    """In a ledger whose phase p1 is frozen, a thin task in the open phase p2 still gets its one enhancement write
    and the master notice naming it."""
    found = _run([{**THIN, "id": "t2", "phase": "p2"}], doc=_frozen("phases/p1"))
    assert found.lines == ["queued enhance-t2 for thin task t2"]
    assert [operation["id"] for operation in _operations(found)] == ["thin-add-t2", "thin-mark-t2"]
    assert found.inbox.sent == [("swarm", f"master@{SLUG}", _notice("enhance-t2"), True)]


@pytest.mark.feature("tick-thin-task-pass")
@pytest.mark.parametrize(
    ("task", "batch"),
    [
        (THIN, ["task_add", "task_enhancement"]),
        (PHASELESS, ["phase_add", "task_update", "task_add", "task_enhancement"]),
    ],
    ids=["phase", "no phase"],
)
def test_every_write_of_a_pass_is_one_the_ledger_api_accepts(task: dict[str, Any], batch: list[str]) -> None:
    """Through the real ledger adapter the first pass over a thin task sends one batch, the enhancement task add and
    the enhancement mark, led for a task with no phase by its own phase add and its move into it, and the ledger API's
    own operation check accepts every operation in it."""
    client = Accepting()
    found = _run([task], cast(FakeLedger, ClientLedger(SLUG, lambda remaining: cast(Any, client))))
    assert client.refused == []
    assert [[operation["op"] for operation in sent] for sent in client.sent] == [batch]
    assert found.lines == ["queued enhance-t1 for thin task t1"]


@pytest.mark.feature("thin-task-enhancement-snapshot")
@pytest.mark.parametrize("held", [{}, {"enhancement": None}, {"enhancement": ""}], ids=["absent", "null", "empty"])
def test_the_thin_mark_carries_the_enhancement_value_read(held: dict[str, Any]) -> None:
    assert thin.unchecked({**THIN, **held}) is True
    found = _run([{**THIN, **held}])
    assert _operations(found)[-1] == {**MARK, "expected": held.get("enhancement")}


@pytest.mark.feature("thin-task-enhancement-snapshot")
@pytest.mark.parametrize("decided", ["planned", "skipped"])
def test_a_revision_retry_preserves_the_thin_snapshot_and_refuses_a_concurrent_decision(
    monkeypatch: pytest.MonkeyPatch, decided: str
) -> None:
    row = {"id": "t1"}
    posts: list[dict[str, Any]] = []
    paths: list[str] = []
    ctx = Mock()
    ctx.refused = []
    client = api_client.ResourceClient("http://ledger.test", {})

    def request(slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        assert slug == SLUG
        paths.append(path)
        if path == "tasks/t1":
            return {"revision": f"{len(posts) + 1:064x}"}
        assert path == "operations"
        assert payload is not None
        posts.append(json.loads(json.dumps(payload)))
        if len(posts) == 1:
            row["enhancement"] = decided
            body = (
                b'{"error": {"code": "revision_conflict", "message": "Resource changed since the expected revision"}}'
            )
            raise urllib.error.HTTPError("http://ledger.test", 409, "Refused", Message(), io.BytesIO(body))
        schemas.check_operations(payload, ledger_server.core, ("t1",))
        [mark] = [operation for operation in payload["ops"] if operation["op"] == "task_enhancement"]
        ledger_enhancement.apply({"tasks": [row]}, mark, ctx)
        return {"rejected": ctx.refused}

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(api_client.time, "sleep", Mock())
    found = _run([THIN], cast(FakeLedger, ClientLedger(SLUG, lambda remaining: client)))
    assert paths == ["tasks/t1", "operations", "tasks/t1", "operations"]
    assert [post["guards"] for post in posts] == [{"tasks/t1": f"{1:064x}"}, {"tasks/t1": f"{2:064x}"}]
    assert [[operation for operation in post["ops"] if operation["op"] == "task_enhancement"] for post in posts] == [
        [{**MARK, "expected": None}],
        [{**MARK, "expected": None}],
    ]
    assert row == {"id": "t1", "enhancement": decided}
    assert found.lines == ["refused the enhancement of t1: ledger sw refused: ['task t1 enhancement changed']"]
    assert found.inbox.sent == []
    ctx.stamp.assert_not_called()
    ctx.record.assert_not_called()
