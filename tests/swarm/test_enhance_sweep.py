"""The enhance sweep queues enhancement for every thin open engineer and ci task through the thin task pass writer,
pacing its writes, and its dry run prints the thin task count and ids per lane and writes nothing."""

import json
from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import Any

import pytest

from scripts.swarm import cli, enhance_sweep
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep

pytestmark = pytest.mark.unit

ANSWERS: dict[str, object] = {"update_task": {"enhancement": "needed"}}
FULL = (
    "Add the sweep seam scripts.swarm.enhance_sweep so every open engineer task whose text is too thin to build from "
    "gets one planner enhancement task through the thin task pass writer, while tasks that already carry a plan "
    "slice or a planned enhancement stay untouched by the sweep. Done when the pull request is merged."
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


@dataclass
class Inbox:
    sent: list[tuple[str, str, str, bool]] = field(default_factory=list[tuple[str, str, str, bool]])

    def send(self, sender: str, address: str, text: str, fyi: bool = False) -> None:
        self.sent.append((sender, address, text, fyi))


def _calls(ledger: FakeLedger) -> list[tuple[str, tuple[object, ...]]]:
    return [(call.operation, call.args) for call in ledger.calls]


def _tick_calls(tasks: list[dict[str, Any]]) -> list[tuple[str, tuple[object, ...]]]:
    import_module("scripts.tick.steps.thin")
    step = {found.name: found for found in registry.steps()}["thin task pass"]
    assert isinstance(step, CallStep)
    ledger = FakeLedger(dict(ANSWERS))
    values: dict[str, Any] = {
        "doc": {"tasks": tasks},
        "tools": replace(fake_tools(), ledger=ledger).for_effects(frozenset({"inbox", "ledger"})),
        "deadline": Deadline.after(5),
        "slug": "sw",
        "inbox": Inbox(),
    }
    step.call(None, values.__getitem__)
    return _calls(ledger)


@pytest.fixture
def swarm(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/home/x/dev/agentihooks", 2, 1))
    pauses: list[float] = []

    def tasks(_: LedgerClient, slug: str) -> list[dict[str, Any]]:
        return TASKS if slug == "sw" else []

    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(LedgerClient, "tasks", tasks)
    monkeypatch.setattr("time.sleep", pauses.append)
    return pauses


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_selects_per_lane_the_thin_tasks_the_tick_step_would() -> None:
    assert enhance_sweep.thin_by_lane(TASKS) == LANES


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_makes_the_same_ledger_writes_as_the_thin_task_pass_on_one_task_list() -> None:
    ledger = FakeLedger(dict(ANSWERS))
    enhance_sweep.sweep(TASKS, ledger, pause=lambda _: None)
    expected = _tick_calls(TASKS)
    assert expected
    assert _calls(ledger) == expected


@pytest.mark.feature("swarm-enhance-sweep")
def test_each_thin_task_writes_under_its_own_thirty_second_deadline() -> None:
    ledger = FakeLedger(dict(ANSWERS))
    enhance_sweep.sweep(TASKS, ledger, pause=lambda _: None)
    deadlines = {id(call.deadline): call.deadline for call in ledger.calls}
    assert len(deadlines) == 5
    assert all(29 < deadline.remaining <= 30 for deadline in deadlines.values())


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_sweep_pauses_three_seconds_between_thin_tasks_and_never_before_the_first() -> None:
    pauses: list[float] = []
    enhance_sweep.sweep(TASKS, FakeLedger(dict(ANSWERS)), pause=pauses.append)
    assert pauses == [3.0, 3.0, 3.0, 3.0]


@pytest.mark.feature("swarm-enhance-sweep")
def test_the_dry_run_prints_the_thin_task_count_and_ids_per_lane_and_writes_nothing(
    swarm: list[float], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[str] = []
    monkeypatch.setattr(enhance_sweep, "ClientLedger", lambda slug, clients: built.append(slug))
    assert cli.main(["sw", "enhance", "sweep", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"dry_run": True, "lanes": REPORT, "actions": []}
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
    assert json.loads(capsys.readouterr().out) == {
        "dry_run": False,
        "lanes": REPORT,
        "actions": [f"queued enhance-{tid} for thin task {tid}" for tid in ("t1", "t2", "t8", "t9", "t10")],
    }
    assert built == [("sw", "clients of sw")]
    assert swarm == [3.0, 3.0, 3.0, 3.0]
    assert _calls(ledger) == _tick_calls(TASKS)
