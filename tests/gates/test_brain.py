"""The brain gate: a covered write needs a consultation receipt bound to the task's current revision (SV2-GAT-04)."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.gates import entry
from scripts.gates.base import Call, Gate, Who
from scripts.gates.brain import (
    EXPIRED,
    FAILED_RESULT,
    NO_RECEIPT,
    OTHER_BRAIN,
    OTHER_TASK,
    REFUSAL,
    STALE_GENERATION,
    STALE_REVISION,
    UNREADABLE,
    UNRELATED,
    BrainGate,
    bind,
    check,
    task_row,
)
from tests.swarm_ledger import legacy_page

FIXTURE = Path(__file__).parents[1] / "fixtures/swarm_v2/consultation-gates.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SWARM, TASK, OTHER = INPUTS["swarm"], *INPUTS["tasks"]
ROWS = INPUTS["rows"]
BRAIN, NOW = "brain-alpha", 1_000.0


def receipt(row="first", task=TASK, result="delivered", expires=NOW + 60, brain=BRAIN, **over):
    return {**bind(task, ROWS[row], brain), "result": result, "expires": expires, **over}


def revision(row):
    spec = {name: ROWS[row][name] for name in ("title", "description", "plan_slice", "plan_lines", "phase")}
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def test_bind_names_purpose_task_spec_revision_generation_and_brain():
    assert bind(TASK, ROWS["first"], BRAIN) == {
        "purpose": "task",
        "task": TASK,
        "revision": revision("first"),
        "generation": 3,
        "brain": BRAIN,
    }


def test_bind_moves_the_revision_only_on_a_spec_change():
    assert bind(TASK, ROWS["changed"], BRAIN)["revision"] == revision("changed") != revision("first")
    assert bind(TASK, ROWS["moved"], BRAIN) == bind(TASK, ROWS["first"], BRAIN)
    assert bind(TASK, ROWS["reclaimed"], BRAIN)["generation"] == 4


def test_bind_reads_missing_spec_fields_as_empty_and_generation_as_zero():
    empty = json.dumps(dict.fromkeys(("description", "phase", "plan_lines", "plan_slice", "title"), ""))
    assert bind(TASK, {}, BRAIN) == {
        "purpose": "task",
        "task": TASK,
        "revision": hashlib.sha256(empty.encode()).hexdigest(),
        "generation": 0,
        "brain": BRAIN,
    }


@pytest.mark.parametrize(
    ("given", "reason"),
    [
        (None, NO_RECEIPT),
        ({}, NO_RECEIPT),
        ({**receipt(), "purpose": "search"}, UNRELATED),
        ({**INPUTS["unrelated"], "expires": NOW + 60}, UNRELATED),
        (receipt(task=OTHER), OTHER_TASK),
        (receipt("reclaimed"), STALE_GENERATION),
        (receipt("changed"), STALE_REVISION),
        (receipt(brain="brain-beta"), OTHER_BRAIN),
        (receipt(expires=NOW), EXPIRED),
        ({k: v for k, v in receipt().items() if k != "expires"}, EXPIRED),
        (receipt(result="failed"), FAILED_RESULT),
        ({k: v for k, v in receipt().items() if k != "result"}, FAILED_RESULT),
        (receipt(), ""),
        (receipt(result="empty"), ""),
        (receipt(expires=NOW + 0.001), ""),
    ],
)
def test_check_names_the_first_reason_a_receipt_fails(given, reason):
    assert check(given, bind(TASK, ROWS["first"], BRAIN), NOW) == reason


def test_a_receipt_without_an_expiry_counts_as_expired_from_time_zero():
    current = bind(TASK, ROWS["first"], BRAIN)
    undated = {k: v for k, v in receipt().items() if k != "expires"}
    assert check(undated, current, 0.5) == EXPIRED
    assert check(receipt(expires=0.6), current, 0.5) == ""


def test_refusals_say_what_failed_and_how_to_recover():
    assert NO_RECEIPT == "no task context was delivered to this session"
    assert UNRELATED == "the receipt answers another purpose than this task"
    assert OTHER_TASK == "the receipt belongs to another task"
    assert STALE_GENERATION == "the receipt predates the current claim of this task"
    assert STALE_REVISION == "the task spec changed after the receipt"
    assert OTHER_BRAIN == "the receipt came from another brain"
    assert EXPIRED == "the receipt expired"
    assert FAILED_RESULT == "the brain failed to answer, so nothing was consulted"
    assert UNREADABLE == "the task spec could not be read"
    assert REFUSAL.format(reason=NO_RECEIPT, task=TASK) == (
        "brain gate: no task context was delivered to this session for task t-alpha; "
        "the next brain refresh delivers the current task context, then write again"
    )


def test_the_gate_registers_as_brain_and_observes_by_default():
    gate = entry.GATES["brain"]
    assert isinstance(gate, BrainGate)
    assert isinstance(gate, Gate)
    assert gate.name == "brain"
    assert gate.default_mode == "observe"


@pytest.mark.parametrize("payload", INPUTS["calls"]["covered"])
def test_covered_writes_match(payload):
    assert BrainGate().matches(Call.from_payload(payload))


@pytest.mark.parametrize("payload", INPUTS["calls"]["ordinary"])
def test_ordinary_calls_never_match(payload):
    assert not BrainGate().matches(Call.from_payload(payload))


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    from scripts.swarm_v2.keyspace import Namespace

    monkeypatch.setattr("hooks.context.project_cache.namespace", lambda: Namespace("install", BRAIN))
    folder = tmp_path / "ledgers"
    folder.mkdir()

    def seed(row="first", task=TASK):
        doc = {"tasks": [{"id": task, **ROWS[row]}, {"id": "t-other", **ROWS["changed"]}]}
        legacy_page.store(folder, SWARM, doc)

    seed()
    return SimpleNamespace(path=folder, seed=seed)


def write(session="s1"):
    return Call.from_payload({**INPUTS["calls"]["covered"][0], "session_id": session})


WHO = Who(name="engineer@100001-0001", swarm=SWARM, lane="eng", task=TASK)


def test_task_row_reads_only_the_named_task(ledgers):
    assert task_row(ledgers.path, SWARM, TASK) == {"id": TASK, **ROWS["first"]}
    assert task_row(ledgers.path, SWARM, "t-missing") == {}
    assert task_row(ledgers.path, "no-such-swarm", TASK) == {}


def test_a_worker_outside_a_task_is_never_checked():
    def refuse(session):
        raise AssertionError("no receipt read outside a task")

    gate = BrainGate(environ={}, receipts=refuse, clock=lambda: NOW)
    assert gate.decide(write(), Who(name="n", swarm=SWARM), None).allowed
    assert gate.decide(write(), Who(name="n", task=TASK), None).allowed


def test_a_first_write_without_a_receipt_is_refused(ledgers):
    seen = []
    gate = BrainGate(
        environ={"LEDGER_DIR": str(ledgers.path)}, receipts=lambda s: seen.append(s) or {}, clock=lambda: NOW
    )
    decision = gate.decide(write("s9"), WHO, None)
    assert not decision.allowed
    assert decision.reason == REFUSAL.format(reason=NO_RECEIPT, task=TASK)
    assert seen == ["s9"]


def test_a_current_receipt_lets_every_repeated_write_through(ledgers):
    gate = BrainGate(environ={"LEDGER_DIR": str(ledgers.path)}, receipts=lambda s: receipt(), clock=lambda: NOW)
    assert all(gate.decide(write(), WHO, None).allowed for _ in range(5))


def test_a_spec_change_refuses_the_old_receipt(ledgers):
    gate = BrainGate(environ={"LEDGER_DIR": str(ledgers.path)}, receipts=lambda s: receipt(), clock=lambda: NOW)
    ledgers.seed("changed")
    assert gate.decide(write(), WHO, None).reason == REFUSAL.format(reason=STALE_REVISION, task=TASK)


def test_an_unreadable_task_refuses(ledgers):
    gate = BrainGate(environ={"LEDGER_DIR": str(ledgers.path)}, receipts=lambda s: receipt(), clock=lambda: NOW)
    who = Who(name="n", swarm=SWARM, task="t-missing")
    assert gate.decide(write(), who, None).reason == REFUSAL.format(reason=UNREADABLE, task="t-missing")


def test_the_gate_reads_the_process_environment_and_wall_clock_by_default(ledgers, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(ledgers.path))
    fresh = BrainGate(receipts=lambda s: receipt(expires=4_000_000_000.0))
    assert fresh.decide(write(), WHO, None).allowed
    old = BrainGate(receipts=lambda s: receipt(expires=NOW))
    assert old.decide(write(), WHO, None).reason == REFUSAL.format(reason=EXPIRED, task=TASK)


def test_the_gate_reads_the_delivered_consultation_by_default(ledgers, monkeypatch):
    monkeypatch.setattr("hooks.context.task_context.consultation", lambda session: receipt() if session == "s1" else {})
    gate = BrainGate(environ={"LEDGER_DIR": str(ledgers.path)}, clock=lambda: NOW)
    assert gate.decide(write("s1"), WHO, None).allowed
    assert not gate.decide(write("s2"), WHO, None).allowed
