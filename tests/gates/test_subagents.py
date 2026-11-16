import io
import json

import pytest

from scripts.gates import Call, Gate, Who, entry, modes
from scripts.gates.budget import Budget
from scripts.gates.subagents import SubagentBudget, refusal
from scripts.gates.verdicts import Verdicts
from scripts.swarm.store import SwarmError
from tests.swarm.review_ledger import ReviewLedger

ME = Who(name="engineer@1-1", swarm="demo", lane="eng", task="t1")


def state(tmp_path):
    return Verdicts("demo", "subagents", tmp_path)


def rows(tmp_path):
    path = tmp_path / "demo" / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def launch(kind="general-purpose", tool="Agent"):
    return Call(tool, {"subagent_type": kind, "description": "read", "prompt": "go"})


def test_it_is_a_gate_that_enforces_by_default():
    gate = SubagentBudget()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode, gate.launches, gate.continuations) == ("subagents", "enforce", 8, 8)


def test_a_swarm_config_naming_no_mode_denies_the_launch_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setitem(entry.GATES, "subagents", SubagentBudget(launches=1, continuations=1))
    monkeypatch.setattr(modes, "swarm_gates", lambda swarm, environ: {})
    environ = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": ME.name, "AGENTIHOOKS_SWARM_TASK": "t1"}
    payload = json.dumps({"tool_name": "Agent", "tool_input": {"prompt": "go"}, "session_id": "sid-1"})
    codes = [entry.main(["subagents"], io.StringIO(payload), environ, tmp_path) for _ in range(2)]
    assert codes == [0, 2]
    assert [r["kind"] for r in rows(tmp_path)] == ["count", "deny"]


def test_it_matches_launch_and_continuation_tools_only():
    gate = SubagentBudget()
    assert [gate.matches(Call(tool)) for tool in ("Agent", "Task", "SendMessage", "Bash", "Read", "")] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]


def test_a_third_launch_is_refused_whatever_the_sub_agents_name(tmp_path):
    gate = SubagentBudget(launches=2, continuations=2)
    calls = [launch("standards-reader"), launch("Explore", tool="Task"), launch("anything-else")]
    decisions = [gate.decide(call, ME, state(tmp_path)) for call in calls]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[2].reason == refusal("demo", "t1", "launches", 2)


def test_a_third_continuation_is_refused_and_launches_keep_their_own_count(tmp_path):
    gate = SubagentBudget(launches=2, continuations=2)
    decide = lambda tool: gate.decide(Call(tool, {"to": "reader"}), ME, state(tmp_path)).allowed  # noqa: E731
    assert [decide("SendMessage") for _ in range(3)] == [True, True, False]
    assert decide("Agent") is True


def test_each_allowed_call_writes_a_count_row_and_a_refused_one_does_not(tmp_path):
    gate = SubagentBudget(launches=1, continuations=1)
    gate.decide(launch(), ME, state(tmp_path))
    gate.decide(Call("SendMessage"), ME, state(tmp_path))
    gate.decide(launch(), ME, state(tmp_path))
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"], r["reason"]) for r in rows(tmp_path)] == [
        ("subagents", "count", ME.name, "t1", "Agent", "sub-agent launches 1 of 1 for task t1"),
        ("subagents", "count", ME.name, "t1", "SendMessage", "sub-agent continuations 1 of 1 for task t1"),
    ]
    budget = Budget("demo", "subagents", tmp_path)
    assert (budget.spent("t1", "launches"), budget.spent("t1", "continuations")) == (1, 1)


def test_each_task_has_its_own_budget(tmp_path):
    gate = SubagentBudget(launches=1, continuations=1)
    other = Who(name="engineer@1-2", swarm="demo", lane="eng", task="t2")
    assert gate.decide(launch(), ME, state(tmp_path)).allowed
    assert gate.decide(launch(), other, state(tmp_path)).allowed
    assert not gate.decide(launch(), ME, state(tmp_path)).allowed


def test_a_review_reader_launched_twice_is_recorded_once(tmp_path):
    gate = SubagentBudget()
    for _ in range(2):
        gate.decide(Call("Agent", {"name": "spec-reader", "prompt": "go"}), ME, state(tmp_path))
    assert Budget("demo", "subagents", tmp_path).spent("t1", "spec-reader") == 1


def test_a_session_outside_a_swarm_task_is_never_counted(tmp_path):
    gate = SubagentBudget(launches=0, continuations=0)
    for who in (Who(), Who(name="master@1-1", swarm="demo", lane="master"), Who(swarm="demo", task="t1")):
        assert gate.decide(launch(), who, state(tmp_path)).allowed
    assert rows(tmp_path) == []


def test_the_refusal_names_the_spend_the_cap_and_the_way_out():
    assert refusal("demo", "t1", "launches", 8) == (
        "the sub-agent budget for task t1 is spent: 8 of 8 launches. Merge with the ruled findings, file the spec "
        'finding as a follow-up, or block with agentihooks swarm demo block "<why>"'
    )


class Unreachable:
    def task(self, slug: str, task_id: str) -> dict:
        raise OSError("connection refused")


class Refusing(ReviewLedger):
    def update_task(self, *args: object, **kwargs: object) -> dict:
        raise SwarmError("ledger demo: server refused: task_update may set only")


@pytest.mark.feature("review-state-on-ledger")
def test_a_reader_launch_is_recorded_on_the_ledger_task_beside_its_local_counter(tmp_path, monkeypatch):
    ledger = ReviewLedger(slug="demo")
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: ledger)
    gate = SubagentBudget()
    gate.decide(Call("Agent", {"name": "standards-reader", "prompt": "go"}), ME, state(tmp_path))
    gate.decide(Call("Task", {"subagent_type": "spec-reader"}), ME, state(tmp_path))
    gate.decide(launch(), ME, state(tmp_path))
    assert ledger.rows["t1"]["review_readers"] == ["spec-reader", "standards-reader"]
    assert [(write["by"], write["if_match"]) for write in ledger.writes] == [
        (ME.name, {"review_readers": []}),
        (ME.name, {"review_readers": ["standards-reader"]}),
    ]
    budget = Budget("demo", "subagents", tmp_path)
    assert (budget.spent("t1", "standards-reader"), budget.spent("t1", "spec-reader")) == (1, 1)


class Unmoved(ReviewLedger):
    def update_task(self, *args: object, **kwargs: object) -> dict:
        return {}


@pytest.mark.feature("review-state-on-ledger")
@pytest.mark.parametrize(
    ("ledger", "error"),
    [
        (Unreachable(), "connection refused"),
        (Refusing(slug="demo"), "ledger demo: server refused: task_update may set only"),
        (Unmoved(slug="demo"), "reader spec-reader not recorded on task t1: its review readers changed under 5 writes"),
    ],
)
def test_a_reader_launch_the_ledger_cannot_take_is_counted_here_reported_and_allowed(
    tmp_path, monkeypatch, capsys, ledger, error
):
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: ledger)
    decision = SubagentBudget().decide(Call("Agent", {"name": "spec-reader", "prompt": "go"}), ME, state(tmp_path))
    assert decision.allowed
    assert Budget("demo", "subagents", tmp_path).spent("t1", "spec-reader") == 1
    assert [r["kind"] for r in rows(tmp_path)] == ["count"]
    assert capsys.readouterr().err == (
        f"reader launch counted on this machine only, the ledger task did not take it: {error}\n"
    )


class RefusingStandards(ReviewLedger):
    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict,
        by: str = "swarm",
        if_state: tuple[str, ...] = (),
        if_match: dict | None = None,
    ) -> dict:
        if "standards-reader" in fields.get("review_readers", []):
            raise SwarmError("ledger demo: server refused: standards-reader")
        return super().update_task(slug, task_id, fields, by, if_state, if_match)


@pytest.mark.feature("review-state-on-ledger")
def test_one_launch_naming_both_readers_records_the_second_when_the_first_fails(tmp_path, monkeypatch, capsys):
    ledger = RefusingStandards(slug="demo")
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: ledger)
    call = Call("Agent", {"name": "spec-reader", "subagent_type": "standards-reader", "prompt": "go"})
    assert SubagentBudget().decide(call, ME, state(tmp_path)).allowed
    assert ledger.rows["t1"]["review_readers"] == ["spec-reader"]
    assert capsys.readouterr().err == (
        "reader launch counted on this machine only, the ledger task did not take it: "
        "ledger demo: server refused: standards-reader\n"
    )


def test_a_refused_reader_launch_is_not_recorded_on_the_ledger(tmp_path, monkeypatch):
    ledger = ReviewLedger(slug="demo")
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: ledger)
    decision = SubagentBudget(launches=0).decide(Call("Agent", {"name": "spec-reader"}), ME, state(tmp_path))
    assert (decision.allowed, ledger.writes) == (False, [])


def test_a_launch_naming_no_reader_never_reaches_the_ledger(tmp_path, monkeypatch):
    made: list[ReviewLedger] = []
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: made.append(ReviewLedger()) or made[-1])
    assert SubagentBudget().decide(launch(), ME, state(tmp_path)).allowed
    assert made == []
