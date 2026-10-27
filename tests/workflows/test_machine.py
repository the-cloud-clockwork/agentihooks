import dataclasses
import json

import pytest

from scripts.workflows import loader, machine, schema
from scripts.workflows.schema import WorkflowError


def simple(head: str = "build") -> schema.Workflow:
    shipped = loader.read(loader.PACKAGE_WORKFLOWS / "simple-workflow.yaml")
    return dataclasses.replace(shipped, head=head)


def never(text, outputs):
    raise AssertionError(f"exit check called for {text}")


def answers(*replies):
    calls = []

    def check(text, outputs):
        calls.append((text, outputs))
        return replies[len(calls) - 1]

    check.calls = calls
    return check


def run(definition, state, outcomes, exit_check=never):
    for outcome in outcomes:
        state = machine.advance(definition, state, state["stage"], outcome, {"by": state["stage"]}, exit_check)
    return state


def tiny(**stage) -> schema.Workflow:
    return schema.validate(
        {
            "workflow": {"name": "tiny", "intent": "Try.", "head": "try", "exit": "try.pass"},
            "stages": {"try": {"profile": "engineer", "next": {"pass": "exit", "retry": "try"}, **stage}},
        }
    )


def test_start_puts_the_head_stage_first_with_nothing_counted():
    state = machine.start(loader.read(loader.PACKAGE_WORKFLOWS / "simple-workflow.yaml"))

    assert state == {
        "stage": "scout",
        "status": "running",
        "iterations": 0,
        "visit": 0,
        "runs": {},
        "outputs": {},
        "notes": None,
        "action": None,
    }


def test_simple_workflow_with_five_passes_reaches_passed():
    definition = simple(head="scout")
    seen = []
    state = machine.start(definition)
    for _ in range(5):
        seen.append(state["stage"])
        state = machine.advance(definition, state, state["stage"], "pass", {"by": state["stage"]}, answers(True))

    assert seen == ["scout", "evidence-verdict", "plan", "build", "build-verdict"]
    assert state["status"] == "passed"
    assert state["stage"] is None
    assert state["iterations"] == 0
    assert state["outputs"]["build-verdict"] == {"by": "build-verdict"}
    assert json.loads(json.dumps(state)) == state


def test_a_head_at_build_passes_through_build_and_its_verdict():
    state = run(simple(), machine.start(simple()), ["pass", "pass"])

    assert state["status"] == "passed"
    assert state["runs"] == {"build": 1, "build-verdict": 1}


def test_retry_on_build_verdict_three_times_reaches_exhausted_with_the_notes_kept():
    definition = simple()
    state = machine.start(definition)
    for round_ in range(1, 4):
        state = machine.advance(definition, state, "build", "pass", {"diff": round_}, never)
        notes = {"verdict": "retry", "notes": f"round {round_} misses the case"}
        state = machine.advance(definition, state, "build-verdict", "retry", notes, never)

    assert state["status"] == "exhausted"
    assert state["action"] == "block"
    assert state["stage"] is None
    assert state["iterations"] == 3
    assert state["runs"] == {"build": 3, "build-verdict": 3}
    assert state["notes"] == {
        "stage": "build-verdict",
        "outcome": "retry",
        "outputs": {"verdict": "retry", "notes": "round 3 misses the case"},
    }


def test_each_retry_or_enhance_edge_counts_one_iteration():
    definition = simple()
    state = run(definition, machine.start(definition), ["pass", "retry", "pass", "enhance"])

    assert state["status"] == "running"
    assert state["stage"] == "build"
    assert state["iterations"] == 2
    assert state["notes"]["outcome"] == "enhance"


def test_exhaustion_applies_the_workflows_on_exhausted():
    definition = dataclasses.replace(simple(), max_iterations=1, on_exhausted="keep-best")
    state = run(definition, machine.start(definition), ["pass", "retry"])

    assert state["status"] == "exhausted"
    assert state["action"] == "keep-best"


def test_a_stage_past_its_iterations_is_refused():
    definition = tiny(iterations=2)
    state = run(definition, machine.start(definition), ["retry", "retry"])

    assert state["visit"] == 2
    with pytest.raises(WorkflowError, match="try already ran its 2 iterations"):
        machine.advance(definition, state, "try", "pass", {}, never)


def test_a_new_visit_resets_the_stage_count():
    definition = simple()
    state = run(definition, machine.start(definition), ["pass", "retry"])

    assert state["stage"] == "build"
    assert state["visit"] == 0
    assert state["runs"]["build"] == 1


def test_a_free_text_stage_exit_ends_the_stage_when_the_stub_says_yes():
    definition = simple(head="scout")
    check = answers(True)
    state = machine.advance(definition, machine.start(definition), "scout", "retry", {"points": 3}, check)

    assert check.calls == [("every point of the case has cited evidence", {"points": 3})]
    assert state["stage"] == "evidence-verdict"
    assert state["iterations"] == 0
    assert state["notes"] is None


def test_a_free_text_stage_exit_that_says_no_runs_the_stage_again():
    definition = simple(head="scout")
    state = machine.advance(definition, machine.start(definition), "scout", "pass", {"points": 1}, answers(False))

    assert state["stage"] == "scout"
    assert state["visit"] == 1
    assert state["status"] == "running"


def test_a_free_text_stage_exit_out_of_runs_takes_the_reported_outcome():
    definition = simple(head="scout")
    state = run(definition, machine.start(definition), ["pass", "pass"], answers(False, False))

    assert state["stage"] == "evidence-verdict"
    assert state["runs"] == {"scout": 2}


def test_a_stage_pass_workflow_exit_ends_it_before_the_pass_edge():
    definition = schema.validate(
        {
            "workflow": {"name": "early", "intent": "Stop early.", "head": "try", "exit": "try.pass"},
            "stages": {
                "try": {"profile": "engineer", "next": {"pass": "more"}},
                "more": {"profile": "judge", "next": {"pass": "exit"}},
            },
        }
    )
    state = machine.advance(definition, machine.start(definition), "try", "pass", {}, never)

    assert state["status"] == "passed"
    assert state["stage"] is None
    assert state["runs"] == {"try": 1}


def test_a_free_text_workflow_exit_goes_through_the_exit_check_on_a_pass():
    definition = dataclasses.replace(simple(), exit="the change is done")
    check = answers(True)
    state = machine.advance(definition, machine.start(definition), "build", "pass", {"done": True}, check)

    assert check.calls == [("the change is done", {"build": {"done": True}})]
    assert state["status"] == "passed"
    assert state["stage"] is None


def test_a_free_text_workflow_exit_is_not_asked_on_a_retry():
    definition = dataclasses.replace(simple(), exit="the change is done")
    state = run(definition, machine.start(definition), ["pass"], answers(False))
    state = machine.advance(definition, state, "build-verdict", "retry", {}, never)

    assert state["status"] == "running"
    assert state["stage"] == "build"
    assert state["iterations"] == 1


def test_an_exit_edge_passes_the_workflow_when_its_exit_says_no():
    definition = dataclasses.replace(simple(), exit="the change is done")
    check = answers(False, False)
    state = run(definition, machine.start(definition), ["pass", "pass"], check)

    assert len(check.calls) == 2
    assert state["status"] == "passed"
    assert state["stage"] is None


def test_the_state_keeps_its_own_copy_of_the_outputs():
    definition = simple()
    state = machine.advance(definition, machine.start(definition), "build", "pass", {"diff": 1}, never)
    notes = {"notes": ["misses the case"]}
    state = machine.advance(definition, state, "build-verdict", "retry", notes, never)
    notes["notes"].append("changed after")

    assert state["outputs"]["build-verdict"] == {"notes": ["misses the case"]}
    assert state["notes"]["outputs"] == {"notes": ["misses the case"]}


def test_advance_never_changes_the_state_it_was_given():
    definition = simple()
    state = machine.start(definition)
    before = json.loads(json.dumps(state))

    machine.advance(definition, state, "build", "pass", {"diff": 1}, never)

    assert state == before


@pytest.mark.parametrize(
    ("stage", "outcome", "message"),
    [
        ("plan", "pass", "plan is not the current stage build"),
        ("build", "fail", "fail is not one of pass, retry, enhance"),
        ("build", "retry", "build has no retry edge"),
    ],
)
def test_a_wrong_advance_is_refused(stage, outcome, message):
    definition = simple()

    with pytest.raises(WorkflowError, match=message):
        machine.advance(definition, machine.start(definition), stage, outcome, {}, never)


def test_a_finished_workflow_refuses_to_advance():
    definition = simple()
    state = run(definition, machine.start(definition), ["pass", "pass"])

    with pytest.raises(WorkflowError, match="the workflow is passed"):
        machine.advance(definition, state, "build", "pass", {}, never)
