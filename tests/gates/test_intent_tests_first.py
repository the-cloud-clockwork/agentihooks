import json
import subprocess
from types import SimpleNamespace

import pytest

from hooks.classifier import YesNo
from scripts.gates import intent
from tests.gates.test_intent import WHO, bash, check, verdicts

DECLARATION = "Task part: tests-first"
DOC = {
    "overview": "CI under fifteen minutes.",
    "phases": [{"id": "p1", "title": "CI", "description": "Reject weak gates."}],
}
TASK = {
    "title": "Tighten the gate",
    "description": "Reject a missing required check.",
    "phase": "p1",
}


def state(body=DECLARATION):
    return intent.state_of(
        DOC,
        TASK,
        {
            "title": "Accept old and new gate states",
            "body": body,
            "files": ["tests/gates/test_required.py"],
            "diff": "+assert result in {old_state, new_state}",
        },
    )


def classifier(seen, **probabilities):
    def decide(value, questions, purpose):
        seen.append((value, questions, purpose))
        return SimpleNamespace(
            answers={
                key: SimpleNamespace(
                    noul=probabilities.get(key, 0.1 if key in {"weakens", "changes_gate_behavior"} else 0.9)
                )
                for key in questions
            }
        )

    return decide


def test_declared_preparatory_pull_request_uses_its_contract_with_the_whole_task_as_context():
    value = state("Closes #1\n\n" + DECLARATION + "\n")
    seen = []
    assert intent.judge(value, decide=classifier(seen))[0] == "pass"
    [(judged, questions, purpose)] = seen
    assert judged["task_part"] == "tests-first"
    assert judged["task_text"] == TASK["description"]
    assert judged["pull_request_diff"] == "+assert result in {old_state, new_state}"
    assert purpose == "intent-check"
    assert set(questions) == {
        "usable",
        "delivers",
        "reachable",
        "weakens",
        "accepts_both_states",
        "changes_gate_behavior",
    }
    assert all(isinstance(question, YesNo) for question in questions.values())
    assert all(question.true and question.false and question.true != question.false for question in questions.values())
    assert "tests first" in questions["usable"].instructions
    assert "old and new" in questions["accepts_both_states"].instructions
    assert "gate behaviour" in questions["changes_gate_behavior"].instructions


@pytest.mark.parametrize(
    "body", ["", "This is tests-first", "Task part: implementation", "> " + DECLARATION, DECLARATION + " later"]
)
def test_ordinary_and_later_pull_requests_keep_the_whole_task_contract(body):
    value = state(body)
    assert "task_part" not in value
    assert "pull_request_diff" not in value
    seen = []
    assert intent.judge(value, decide=classifier(seen, usable=0.1))[0] == "fail"
    assert set(seen[0][1]) == {"usable", "delivers", "reachable", "weakens"}


@pytest.mark.parametrize("probability", [0.5, 0.99])
def test_declared_tests_first_changes_to_gate_behavior_fail_even_when_the_phase_can_use_them(probability):
    verdict, reason = intent.judge(state(), decide=classifier([], changes_gate_behavior=probability))
    assert verdict == "fail"
    assert "gate behaviour" in reason
    assert "Implement the missing acceptance behavior" not in reason
    assert "Accept both old and new gate states in the preparatory tests" in reason
    assert "Deliver the gate implementation in the later pull request." in reason
    assert f"Deliver {TASK['title']}" not in reason


def test_preparatory_tests_must_accept_both_states_even_when_other_answers_pass():
    verdict, reason = intent.judge(state(), decide=classifier([], accepts_both_states=0.49))
    assert verdict == "fail"
    assert "old and new" in reason


def test_preparatory_part_is_not_failed_for_leaving_the_later_plan_implementation_unfinished():
    value = {**state(), "plan_lines": "1-1", "plan_chunk": "1: Reject a missing required check."}
    seen = []
    assert intent.judge(value, decide=classifier(seen))[0] == "pass"
    assert "underdelivers" not in seen[0][1]


def test_declared_part_reads_the_actual_diff_from_the_same_head():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["pr", "view"]:
            output = '{"title":"Tests first","body":"Task part: tests-first","files":[{"path":"tests/gates/test_required.py"}]}'
        elif args[1:3] == ["pr", "diff"]:
            output = "+assert result in {old_state, new_state}"
        elif ".head.sha" in args:
            output = "head\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output)

    pr = intent.pr_view("https://github.com/org/repo/pull/1", run=run)
    assert pr["diff"] == "+assert result in {old_state, new_state}"
    assert ["gh", "pr", "diff", "https://github.com/org/repo/pull/1"] in calls


def pull_request_runner(diff, failure=None, moved=False):
    heads = iter(["head", "moved" if moved else "head"])

    def run(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            output = json.dumps(
                {"title": "Tests first", "body": DECLARATION, "files": [{"path": "tests/gates/test_required.py"}]}
            )
        elif args[1:3] == ["pr", "diff"]:
            if isinstance(failure, Exception):
                raise failure
            return SimpleNamespace(returncode=failure or 0, stdout=diff)
        elif ".head.sha" in args:
            output = next(heads)
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output)

    return run


def checked_part(tmp_path, runner, seen):
    task = {**TASK, "id": WHO.task, "state": "pr", "pr_url": "https://github.com/org/repo/pull/1"}
    doc = {**DOC, "tasks": [task]}
    check(
        tmp_path,
        view=lambda url: intent.pr_view(url, run=runner),
        ask=lambda value: intent.judge(value, decide=classifier(seen)),
    ).run(doc)
    return verdicts(tmp_path).read(WHO.task)


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired("gh", 30), OSError("unavailable")])
def test_failed_diff_retrieval_records_failure_and_denies_merge_after_the_grace_period(tmp_path, failure):
    seen = []
    record = checked_part(tmp_path, pull_request_runner("", failure=failure), seen)
    assert record["verdict"] == "fail"
    assert "complete pull request diff" in record["reason"]
    assert not seen
    decision = intent.IntentGate(clock=lambda: 1e15).decide(bash("gh pr merge 1 --squash"), WHO, verdicts(tmp_path))
    assert not decision.allowed


def test_production_check_judges_a_complete_large_diff_before_the_history_bound(tmp_path):
    diff = "+assert result in {old_state, new_state}\n" * 300 + "+last assertion\n"
    seen = []
    record = checked_part(tmp_path, pull_request_runner(diff), seen)
    assert record["verdict"] == "pass"
    assert seen[0][0]["pull_request_diff"] == diff


def test_a_push_during_preparatory_diff_retrieval_discards_the_snapshot():
    assert (
        intent.pr_view("https://github.com/org/repo/pull/1", run=pull_request_runner("+old and new", moved=True))
        is None
    )


def test_compatibility_and_gate_change_thresholds_allow_the_valid_side():
    assert (
        intent.judge(state(), decide=classifier([], accepts_both_states=0.5, changes_gate_behavior=0.49))[0] == "pass"
    )
