"""The enhance sweep queues enhancement for every thin open engineer and ci task through the thin task pass writer,
pacing its writes, and its dry run prints the thin task count and ids per lane and writes nothing."""

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

import pytest

from scripts.swarm import cli, enhance_sweep, ledger_client
from scripts.swarm.ledger_client import LedgerOffline, LedgerRefused, LedgerUnreachable
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep, thin

pytestmark = pytest.mark.unit

ANSWERS: dict[str, object] = {"update_task": {"enhancement": "needed"}}
FULL = (
    "Add the sweep seam scripts.swarm.enhance_sweep so every open engineer task whose text is too thin to build from "
    "gets one planner enhancement task through the thin task pass writer, while tasks that already carry a plan "
    "slice or a planned enhancement stay untouched by the sweep. "
    "Tests observe scripts.swarm.enhance_sweep writes one planner enhancement per thin task. "
    "Done when the pull request is merged."
)


def _task(tid: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": f"Task {tid}",
        "state": "open",
        "lane": "eng",
        "kind": "code",
        "phase": "p1",
        "description": "Fix it.",
        **fields,
    }


TASKS = [
    _task("t1"),
    _task("t2", lane="ci"),
    _task("t3", description=FULL, territory=["scripts/swarm"]),
    _task("t4", state="done"),
    _task("t5", lane="plan", kind="plan"),
    _task("t6", plan_slice="slices/plan.t6"),
    _task("t7", enhancement="needed"),
    _task("t8"),
    _task("enhance-t8", lane="plan", kind="plan", enhances="t8"),
    {key: value for key, value in _task("t9").items() if key != "lane"},
    {key: value for key, value in _task("t10").items() if key != "description"},
]
LANES = {"eng": ["t1", "t8", "t9", "t10"], "ci": ["t2"]}
REPORT = {"eng": {"count": 4, "ids": ["t1", "t8", "t9", "t10"]}, "ci": {"count": 1, "ids": ["t2"]}}
HELD = _task("t11", phase="p2")
SLICED = _task("t11", phase="p3", slice="slices/s2")
MERGED = [_task("t11", merged_into="t13"), _task("t13", phase="p2", state="done")]
UNFROZEN = thin.unfrozen({"tasks": TASKS})


@dataclass
class Inbox:
    sent: list[tuple[str, str, str, bool]] = field(default_factory=list[tuple[str, str, str, bool]])

    def send(self, sender: str, address: str, text: str, fyi: bool = False) -> None:
        self.sent.append((sender, address, text, fyi))


THREE = [_task("a"), _task("b"), _task("c")]


@dataclass
class SecondWriteFails(FakeLedger):
    error: Exception = field(
        default_factory=lambda: TimeoutError("the ledger writer took tasks on sw and sent no answer")
    )
    writes: int = 0
    log: list[str] = field(default_factory=list[str])
    deadlines: list[Deadline] = field(default_factory=list[Deadline])

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object:
        self.writes += 1
        self.log.append(f"write {self.writes}")
        self.deadlines.append(deadline)
        if self.writes == 2:
            raise self.error
        return super().mutate(operations, deadline=deadline)


def _calls(ledger: FakeLedger) -> list[tuple[str, tuple[object, ...]]]:
    return [(call.operation, call.args) for call in ledger.calls]


def _tick_calls(
    tasks: list[dict[str, Any]], held: dict[str, list[dict[str, Any]]] | None = None
) -> list[tuple[str, tuple[object, ...]]]:
    import_module("scripts.tick.steps.thin")
    step = {found.name: found for found in registry.steps()}["thin task pass"]
    assert isinstance(step, CallStep)
    ledger = FakeLedger(dict(ANSWERS))
    values: dict[str, Any] = {
        "doc": {**(held or {}), "tasks": tasks},
        "tools": replace(fake_tools(), ledger=ledger).for_effects(frozenset({"inbox", "ledger"})),
        "deadline": Deadline.after(5),
        "slug": "sw",
        "inbox": Inbox(),
    }
    step.call(None, values.__getitem__)
    return _calls(ledger)


def _serve(monkeypatch: pytest.MonkeyPatch, collections: dict[str, list[dict[str, Any]]]) -> None:
    def resource(slug: str, path: str, collection: bool = False) -> list[dict[str, Any]]:
        return collections.get(path, []) if (slug, collection) == ("sw", True) else []

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(resource=resource))


def _frozen(target: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "plans": [{"id": "a", "title": "Claims"}, {"id": "b", "title": "Hives"}],
        "phases": [{"id": "p1", "title": "Build", "plan": "plans/a"}, {"id": "p2", "title": "Ship", "plan": "plans/b"}],
        "slices": [{"id": "s2", "title": "Cut", "phase": "phases/p2"}],
        "freezes": [
            {"id": f"freeze-{target}", "verb": "freeze", "target": target, "by": "operator", "at": 1, "reason": ""}
        ],
    }


@pytest.fixture
def swarm(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/home/x/dev/agentihooks", 2, 1))
    pauses: list[float] = []
    monkeypatch.setattr(cli, "connect", lambda: store)
    _serve(monkeypatch, {"tasks": TASKS})
    monkeypatch.setattr("time.sleep", pauses.append)
    return pauses


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_selects_per_lane_the_thin_tasks_the_tick_step_would() -> None:
    assert enhance_sweep.thin_by_lane(TASKS, UNFROZEN) == LANES


@pytest.mark.feature("thin-skip-open-pr")
def test_the_sweep_skips_a_thin_task_that_carries_a_pull_request_or_a_branch() -> None:
    tasks = [
        _task("t1", pr_url="https://github.com/o/r/pull/7"),
        _task("t2", lane="ci", branch="engineer-1"),
        _task("t3", pr_url="", branch=""),
    ]
    assert enhance_sweep.thin_by_lane(tasks, thin.unfrozen({"tasks": tasks})) == {"eng": ["t3"], "ci": []}


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_makes_the_same_ledger_writes_as_the_thin_task_pass_on_one_task_list() -> None:
    ledger = FakeLedger(dict(ANSWERS))
    enhance_sweep.sweep(TASKS, UNFROZEN, ledger, pause=lambda _: None)
    expected = _tick_calls(TASKS)
    assert expected
    assert _calls(ledger) == expected


@pytest.mark.feature("swarm-enhance-sweep")
def test_each_thin_task_writes_under_its_own_thirty_second_deadline() -> None:
    ledger = FakeLedger(dict(ANSWERS))
    enhance_sweep.sweep(TASKS, UNFROZEN, ledger, pause=lambda _: None)
    deadlines = {id(call.deadline): call.deadline for call in ledger.calls}
    assert len(deadlines) == 5
    assert all(29 < deadline.remaining <= 30 for deadline in deadlines.values())


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_pauses_three_seconds_between_thin_tasks_and_never_before_the_first() -> None:
    pauses: list[float] = []
    enhance_sweep.sweep(TASKS, UNFROZEN, FakeLedger(dict(ANSWERS)), pause=pauses.append)
    assert pauses == [3.0, 3.0, 3.0, 3.0]


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_dry_run_prints_the_thin_task_count_and_ids_per_lane_and_writes_nothing(
    swarm: list[float], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[str] = []
    monkeypatch.setattr(enhance_sweep, "ClientLedger", lambda slug, clients: built.append(slug))
    assert cli.main(["sw", "enhance", "sweep", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "dry_run": True,
        "lanes": REPORT,
        "actions": [],
        "counts": {"queued": 0, "failed": 0, "skipped": 0},
    }
    assert captured.err == ""
    assert (built, swarm) == ([], [])


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_queues_each_thin_task_through_the_swarm_ledger_with_paced_writes(
    swarm: list[float], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = FakeLedger(dict(ANSWERS))
    built: list[tuple[str, object]] = []

    def client_ledger(slug: str, clients: object) -> FakeLedger:
        built.append((slug, clients))
        return ledger

    monkeypatch.setattr(enhance_sweep, "ClientLedger", client_ledger)
    monkeypatch.setattr(enhance_sweep, "resource_client", lambda slug: f"clients of {slug}")
    assert cli.main(["sw", "enhance", "sweep"]) == 0
    captured = capsys.readouterr()
    lines = [f"queued enhance-{tid} for thin task {tid}" for tid in ("t1", "t2", "t8", "t9", "t10")]
    assert json.loads(captured.out) == {
        "dry_run": False,
        "lanes": REPORT,
        "actions": lines,
        "counts": {"queued": 5, "failed": 0, "skipped": 0},
    }
    assert captured.err.splitlines() == lines
    assert built == [("sw", "clients of sw")]
    assert swarm == [3.0, 3.0, 3.0, 3.0]
    assert _calls(ledger) == _tick_calls(TASKS)


@pytest.mark.feature("swarm-enhance-sweep")
@pytest.mark.usefixtures("swarm")
def test_an_unfrozen_thin_task_beside_a_frozen_phase_is_still_listed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, {"tasks": [_task("t12", phase="p2")], **_frozen("phases/p1")})
    assert cli.main(["sw", "enhance", "sweep", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["lanes"] == {
        "eng": {"count": 1, "ids": ["t12"]},
        "ci": {"count": 0, "ids": []},
    }


@pytest.mark.feature("swarm-enhance-sweep")
@pytest.mark.parametrize(
    ("target", "added"),
    [
        ("tasks/t11", [HELD]),
        ("phases/p2", [HELD]),
        ("plans/b", [HELD]),
        ("phases/p2", [SLICED]),
        ("phases/p2", MERGED),
    ],
    ids=["task freeze", "phase freeze", "plan freeze", "phase freeze over its slice", "phase freeze over its merge"],
)
def test_the_sweep_lists_and_queues_no_thin_task_a_freeze_holds(
    target: str,
    added: list[dict[str, Any]],
    swarm: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = FakeLedger(dict(ANSWERS))
    held = _frozen(target)
    _serve(monkeypatch, {"tasks": [*TASKS, *added], **held})
    monkeypatch.setattr(enhance_sweep, "ClientLedger", lambda slug, clients: ledger)
    monkeypatch.setattr(enhance_sweep, "resource_client", lambda slug: slug)
    assert cli.main(["sw", "enhance", "sweep"]) == 0
    assert json.loads(capsys.readouterr().out)["lanes"] == REPORT
    assert swarm == [3.0, 3.0, 3.0, 3.0]
    assert _calls(ledger) == _tick_calls([*TASKS, *added], held)


@pytest.mark.feature("swarm-enhance-sweep")
def test_a_refused_enhancement_write_leaves_the_sweep_writing_the_next_thin_task() -> None:
    ledger = SecondWriteFails(dict(ANSWERS), error=LedgerRefused("the task changed"))
    enhance_sweep.sweep(THREE, UNFROZEN, ledger, pause=lambda _: None)
    assert ledger.log == ["write 1", "write 2", "write 3"]
    assert [call.operation for call in ledger.calls] == ["mutate", "mutate"]


@pytest.mark.feature("swarm-enhance-sweep")
@pytest.mark.usefixtures("swarm")
def test_a_sweep_where_every_write_lands_reports_one_queued_line_per_thin_task_in_ledger_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, {"tasks": THREE})
    monkeypatch.setattr(enhance_sweep, "ClientLedger", lambda slug, clients: FakeLedger(dict(ANSWERS)))
    monkeypatch.setattr(enhance_sweep, "resource_client", lambda slug: slug)
    assert cli.main(["sw", "enhance", "sweep"]) == 0
    actions = json.loads(capsys.readouterr().out)["actions"]
    assert actions == [f"queued enhance-{tid} for thin task {tid}" for tid in ("a", "b", "c")]


@pytest.mark.feature("swarm-enhance-sweep-continues-past-timeout")
@pytest.mark.parametrize(
    ("error", "failure"),
    [
        pytest.param(TimeoutError("no answer"), "failed the enhancement of b: TimeoutError no answer", id="timeout"),
        pytest.param(
            OSError("connection lost"), "failed the enhancement of b: OSError connection lost", id="transport"
        ),
        pytest.param(URLError("no route"), "failed the enhancement of b: URLError <urlopen error no route>", id="url"),
        pytest.param(
            LedgerUnreachable("unreachable"),
            "failed the enhancement of b: LedgerUnreachable unreachable",
            id="unreachable",
        ),
        pytest.param(LedgerOffline("offline"), "failed the enhancement of b: LedgerOffline offline", id="offline"),
        pytest.param(LedgerRefused("the task changed"), "refused the enhancement of b: the task changed", id="refused"),
    ],
)
def test_a_failed_second_write_is_counted_and_the_sweep_continues(error: Exception, failure: str) -> None:
    ledger = SecondWriteFails(dict(ANSWERS), error=error)
    pauses: list[float] = []
    result = enhance_sweep.sweep(THREE, UNFROZEN, ledger, pause=pauses.append)
    assert getattr(result, "lines") == (
        "queued enhance-a for thin task a",
        failure,
        "queued enhance-c for thin task c",
    )
    assert getattr(result, "counts") == {"queued": 2, "failed": 1, "skipped": 0}
    assert ledger.log == ["write 1", "write 2", "write 3"]
    assert len(ledger.calls) == 2
    assert pauses == [3.0, 3.0]
    assert len({id(deadline) for deadline in ledger.deadlines}) == 3
    assert all(29 < deadline.remaining <= 30 for deadline in ledger.deadlines)


@pytest.mark.feature("swarm-enhance-sweep-continues-past-timeout")
@pytest.mark.parametrize("error", [ValueError("bad task"), KeyError("missing field")])
def test_the_sweep_leaves_programming_errors_visible(error: Exception) -> None:
    ledger = SecondWriteFails(dict(ANSWERS), error=error)
    with pytest.raises(type(error), match=str(error)):
        enhance_sweep.sweep(THREE, UNFROZEN, ledger, pause=lambda _: None)
    assert ledger.log == ["write 1", "write 2"]


@pytest.mark.feature("swarm-enhance-sweep-continues-past-timeout")
def test_a_frozen_thin_task_is_reported_in_order_without_a_write_or_extra_pause() -> None:
    tasks = [_task("a"), _task("b", phase="p2"), _task("c")]
    held = _frozen("phases/p2")
    unfrozen = thin.unfrozen({"tasks": tasks, **held})
    ledger = FakeLedger(dict(ANSWERS))
    pauses: list[float] = []
    result = enhance_sweep.sweep(tasks, unfrozen, ledger, pause=pauses.append)
    assert getattr(result, "lines") == (
        "queued enhance-a for thin task a",
        "skipped thin task b: held by a freeze",
        "queued enhance-c for thin task c",
    )
    assert getattr(result, "counts") == {"queued": 2, "failed": 0, "skipped": 1}
    assert _calls(ledger) == _tick_calls(tasks, held)
    assert pauses == [3.0]


@pytest.mark.feature("swarm-enhance-sweep-continues-past-timeout")
def test_the_command_flushes_each_outcome_to_stderr_before_the_next_write(
    swarm: list[float], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = SecondWriteFails(dict(ANSWERS), error=TimeoutError("no answer"))
    _serve(monkeypatch, {"tasks": THREE})
    monkeypatch.setattr(enhance_sweep, "ClientLedger", lambda slug, clients: ledger)
    monkeypatch.setattr(enhance_sweep, "resource_client", lambda slug: slug)
    real_print = print

    def printed(*values: object, **options: Any) -> None:
        if options.get("file") is sys.stderr:
            assert options.get("flush") is True
            ledger.log.append(str(values[0]))
        real_print(*values, **options)

    monkeypatch.setattr("builtins.print", printed)
    assert cli.main(["sw", "enhance", "sweep"]) == 0
    captured = capsys.readouterr()
    lines = [
        "queued enhance-a for thin task a",
        "failed the enhancement of b: TimeoutError no answer",
        "queued enhance-c for thin task c",
    ]
    assert captured.err.splitlines() == lines
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out) == {
        "dry_run": False,
        "lanes": {"eng": {"count": 3, "ids": ["a", "b", "c"]}, "ci": {"count": 0, "ids": []}},
        "actions": lines,
        "counts": {"queued": 2, "failed": 1, "skipped": 0},
    }
    assert ledger.log == ["write 1", lines[0], "write 2", lines[1], "write 3", lines[2]]
    assert swarm == [3.0, 3.0]


@pytest.mark.feature("swarm-enhance-sweep-continues-past-timeout")
@pytest.mark.parametrize("dry_run", [True, False])
def test_the_command_reports_frozen_skips_without_constructing_a_writer_in_a_dry_run(
    dry_run: bool, swarm: list[float], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = [_task("b", phase="p2")]
    _serve(monkeypatch, {"tasks": tasks, **_frozen("phases/p2")})
    built: list[str] = []
    ledger = FakeLedger(dict(ANSWERS))

    def client_ledger(slug: str, clients: object) -> FakeLedger:
        built.append(slug)
        return ledger

    monkeypatch.setattr(enhance_sweep, "ClientLedger", client_ledger)
    monkeypatch.setattr(enhance_sweep, "resource_client", lambda slug: slug)
    assert cli.main(["sw", "enhance", "sweep", *(["--dry-run"] if dry_run else [])]) == 0
    captured = capsys.readouterr()
    line = "skipped thin task b: held by a freeze"
    assert captured.err == line + "\n"
    assert json.loads(captured.out) == {
        "dry_run": dry_run,
        "lanes": {"eng": {"count": 0, "ids": []}, "ci": {"count": 0, "ids": []}},
        "actions": [line],
        "counts": {"queued": 0, "failed": 0, "skipped": 1},
    }
    assert built == ([] if dry_run else ["sw"])
    assert ledger.calls == []
    assert swarm == []
