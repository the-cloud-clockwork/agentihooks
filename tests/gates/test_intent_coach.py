from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.gates import intent, modes
from scripts.gates.base import Call, Who
from scripts.gates.verdicts import Verdicts
from tests.gates.test_intent import DOC, ME, NOW, PR, SLUG, TASK, Ledger, Mail, rows


def run_check(tmp_path, head, verdict="fail", reason="missing behavior"):
    ledger, mail = Ledger(), Mail()
    check = intent.Check(
        SLUG,
        "coach",
        NOW,
        ledger,
        mail,
        lambda url: {**PR, "head": head},
        lambda state: (verdict, reason),
        home=tmp_path,
    )
    check.run(DOC)
    return ledger, mail


def gate(tmp_path, command="gh pr merge 9 --squash"):
    who = Who(name=ME, swarm=SLUG, task=TASK)
    state = Verdicts(SLUG, "intent", tmp_path)
    with (
        patch.object(intent, "pr_head", return_value=state.read(TASK).get("head")),
        patch.object(intent, "pr_merged", return_value="done" in command),
    ):
        return intent.IntentGate().decide(Call("Bash", {"command": command}), who, state, mode="coach")


def test_miss_coaches_then_a_fixed_head_passes(tmp_path):
    ledger, mail = run_check(tmp_path, "first")
    assert not gate(tmp_path).allowed
    text = mail.sent[0][2]
    assert "missing behavior" in text
    assert "Fix steps:" in text
    assert "fix round 1 of 2" in text
    assert ledger.updates == [(SLUG, TASK, {"state": "claimed"}, "swarm")]
    assert ledger.comments[0][2] == text
    ledger, mail = run_check(tmp_path, "fixed", "pass", "meets intent")
    assert gate(tmp_path).allowed
    assert (ledger.updates, ledger.comments, mail.sent) == ([], [], [])
    assert Verdicts(SLUG, "intent", tmp_path).read(TASK)["verdict"] == "pass"


@pytest.mark.parametrize("command", ["gh pr merge 9 --squash", f"agentihooks swarm {SLUG} done --pr x"])
def test_two_failed_fix_rounds_allow_merge_and_done_and_record_shortfall(tmp_path, command):
    run_check(tmp_path, "original")
    assert not gate(tmp_path, command).allowed
    ledger, mail = run_check(tmp_path, "fix-one")
    assert "fix round 2 of 2" in mail.sent[0][2]
    assert not gate(tmp_path, command).allowed
    ledger, mail = run_check(tmp_path, "fix-two")
    assert gate(tmp_path, command).allowed
    assert ledger.updates == []
    assert ledger.comments == [
        (
            SLUG,
            TASK,
            "Intent remains unmet after two fix rounds: missing behavior. The master must review this shortfall.",
            "swarm",
        )
    ]
    outcome = "merged" if "done" in command else "merge permitted"
    assert rows(tmp_path)[-1]["reason"] == f"{outcome} with intent unmet after two fix rounds: missing behavior"


def test_same_head_and_rearming_cannot_consume_a_fix_round(tmp_path, monkeypatch):
    run_check(tmp_path, "original")
    monkeypatch.setattr(intent, "stamp_body", lambda *args: True)
    intent.stamp(SLUG, TASK, "url", DOC, "coach", NOW, tmp_path)
    ledger, mail = run_check(tmp_path, "original")
    assert not gate(tmp_path).allowed
    assert (ledger.updates, ledger.comments, mail.sent) == ([], [], [])
    assert Verdicts(SLUG, "intent", tmp_path).read(TASK)["coach_rounds"] == 0


def test_claimed_task_rechecks_after_push_without_rearming(tmp_path):
    run_check(tmp_path, "original")
    ledger, mail = Ledger(), Mail()
    doc = {**DOC, "tasks": [{**DOC["tasks"][0], "state": "claimed"}]}
    check = intent.Check(
        SLUG,
        "coach",
        NOW,
        ledger,
        mail,
        lambda url: {**PR, "head": "fixed"},
        lambda state: ("pass", "ok"),
        home=tmp_path,
    )
    check.run(doc)
    assert gate(tmp_path).allowed
    assert ledger.updates == [(SLUG, TASK, {"state": "pr"}, "swarm")]


def test_coach_is_an_intent_mode():
    assert intent.mode_of(SimpleNamespace(gates={"intent": "coach"})) == "coach"
    assert modes.configured(intent.IntentGate(), {"intent": "coach"}) == "coach"


def test_deny_feedback_contains_fix_steps(tmp_path):
    Verdicts(SLUG, "intent", tmp_path).write(TASK, "fail", "missing behavior", NOW)
    decision = gate(tmp_path)
    assert not decision.allowed
    assert "missing behavior" in decision.reason
    assert "Fix steps:" in decision.reason


def test_a_new_head_cannot_merge_on_an_old_pass(tmp_path, monkeypatch):
    run_check(tmp_path, "passed", "pass", "ok")
    monkeypatch.setattr(intent, "pr_head", lambda url: "new")
    state = Verdicts(SLUG, "intent", tmp_path)
    decision = intent.IntentGate(clock=lambda: NOW / 1000).decide(
        Call("Bash", {"command": "gh pr merge 9"}), Who(name=ME, swarm=SLUG, task=TASK), state, mode="coach"
    )
    assert decision.reason == "Intent must be checked on the new head. Wait for the tick to rerun the check."
    assert not decision.allowed
    assert state.read(TASK) == {"verdict": "pending", "reason": "intent check running", "at": NOW}


def test_push_during_snapshot_discards_old_proof():
    import json

    from tests.gates.test_intent import Ran

    raw = {"title": "Old", "body": "old proof", "files": [{"path": "old.py"}]}
    ran = Ran((0, "old"), (0, json.dumps(raw)), (0, ""), (0, "new"))
    assert intent.pr_view("https://github.com/o/r/pull/9", run=ran) is None
    assert ran.calls[0][0][-1] == ".head.sha"
    assert ran.calls[-1][0][-1] == ".head.sha"


def test_remediation_names_the_task_seam_and_reaches_every_feedback_channel(tmp_path):
    from tests.gates.test_intent import classifier

    state = {
        "task": "Publish status command",
        "task_text": "The swarm status command shows the active controller.",
        "phase": "Remote control",
        "phase_intent": "Workers report their controller.",
    }
    verdict, reason = intent.judge(state, decide=classifier(0.1, delivers=0.1, reachable=0.1, weakens=0.8))
    assert verdict == "fail"
    assert (
        "Implement the missing acceptance behavior described by Publish status command: The swarm status command shows the active controller."
        in reason
    )
    assert (
        "Wire the production entrypoint for Publish status command and prove an invocation delivers Remote control: Workers report their controller."
        in reason
    )
    assert "Preserve Remote control: Workers report their controller." in reason
    ledger, mail = run_check(tmp_path, "original", reason=reason)
    assert reason in mail.sent[0][2]
    assert reason in gate(tmp_path).reason
    run_check(tmp_path, "fix-one", reason=reason)
    ledger, mail = run_check(tmp_path, "fix-two", reason=reason)
    assert ledger.comments == [
        (
            SLUG,
            TASK,
            f"Intent remains unmet after two fix rounds: {reason}. The master must review this shortfall.",
            "swarm",
        )
    ]


@pytest.mark.parametrize("command", ["gh pr merge 9 --squash", f"agentihooks swarm {SLUG} done --pr x"])
def test_coach_mode_runs_through_the_real_gate_entry(tmp_path, monkeypatch, capsys, command):
    import io
    import json

    from scripts.gates import entry

    monkeypatch.setattr(modes, "swarm_gates", lambda *args: {"intent": "coach"})
    monkeypatch.setattr(intent, "pr_head", lambda url: "original")
    monkeypatch.setattr(intent, "pr_merged", lambda url: "done" in command)
    run_check(tmp_path, "original")
    env = {"AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_SWARM_TASK": TASK}
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert entry.main(["intent"], io.StringIO(json.dumps(payload)), env, tmp_path) == 2
    assert "fix round 1 of 2" in capsys.readouterr().err
    run_check(tmp_path, "fix-one")
    run_check(tmp_path, "fix-two")
    monkeypatch.setattr(intent, "pr_head", lambda url: "fix-two")
    assert entry.main(["intent"], io.StringIO(json.dumps(payload)), env, tmp_path) == 0
    assert capsys.readouterr().err == ""


def test_coach_mode_cannot_be_set_for_another_gate():
    from scripts.gates import entry
    from scripts.swarm.cli import gate_mode
    from scripts.swarm.store import SwarmError

    assert modes.configured(entry.GATES["watch"], {"watch": "coach"}) == "enforce"
    with pytest.raises(SwarmError):
        gate_mode("watch-gate", "coach", {"AGENTIHOOKS_AGENT_NAME": "operator"})
    assert gate_mode("intent-gate", "coach", {"AGENTIHOOKS_AGENT_NAME": "operator"}) == {"intent": "coach"}


def test_coach_mode_is_accepted_by_the_control_endpoint():
    from scripts.swarm_ledger.ledger_server import control_argv

    assert control_argv({"action": "set", "gates": {"intent": "coach"}}) == ["set", "intent-gate=coach"]
    with pytest.raises(ValueError):
        control_argv({"action": "set", "gates": {"watch": "coach"}})


@pytest.mark.parametrize("result", [None, "blank", "failed", "timeout", "error"])
def test_unavailable_head_is_not_judged(tmp_path, result):
    import subprocess

    def run(args, **kwargs):
        if result == "timeout":
            raise subprocess.TimeoutExpired(args, 1)
        if result == "error":
            raise OSError("down")
        return subprocess.CompletedProcess(
            args, 1 if result == "failed" else 0, stdout="" if result == "blank" else "head"
        )

    if result is None:
        assert intent.pr_head("https://example.com", run=run) is None
    else:
        expected = "head" if result not in ("blank", "failed", "timeout", "error") else None
        assert (intent.pr_head("https://github.com/o/r/pull/9", run=run) or None) == expected
