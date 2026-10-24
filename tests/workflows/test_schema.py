import copy

import pytest
import yaml

from scripts.workflows import schema
from scripts.workflows.schema import WorkflowError

BASE = {
    "workflow": {
        "name": "tiny",
        "intent": "Build what the case asks.",
        "head": "build",
        "exit": "verdict.pass",
    },
    "stages": {
        "build": {
            "profile": "engineer",
            "input": ["case"],
            "next": {"pass": "verdict"},
        },
        "verdict": {
            "profile": "judge",
            "tier": "low",
            "input": ["case", "build"],
            "filters": ["judge-blind"],
            "next": {"pass": "exit", "retry": "build", "enhance": "build"},
        },
    },
}


def document(**changes):
    doc = copy.deepcopy(BASE)
    for path, value in changes.items():
        target = doc
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return doc


def block(doc) -> str:
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)


def refused(text: str) -> str:
    with pytest.raises(WorkflowError) as caught:
        schema.parse(text)
    return "\n".join(caught.value.errors)


def test_a_valid_file_loads_with_defaults_filled_in():
    workflow = schema.parse(block(BASE))

    assert workflow.name == "tiny"
    assert workflow.max_iterations == 3
    assert workflow.on_exhausted == "block"
    build = workflow.stages["build"]
    assert build.tier == "high"
    assert build.effort == "medium"
    assert build.count == 1
    assert build.input == ("case",)
    assert build.filters == ()
    assert build.iterations == 1
    assert build.exit is None
    assert build.next == {"pass": "verdict", "retry": None, "enhance": None}
    assert workflow.stages["verdict"].tier == "low"


def test_the_document_round_trips_with_every_key_present():
    workflow = schema.parse(block(BASE))

    out = workflow.to_document()

    assert list(out["workflow"]) == list(schema.WORKFLOW_KEYS)
    assert list(out["stages"]["build"]) == list(schema.STAGE_KEYS)
    assert schema.parse(block(out)) == workflow


def test_a_stage_with_an_unknown_key_is_refused():
    errors = refused(block(document(stages__build__model="opus")))

    assert "stages.build: unknown key model" in errors


def test_an_unknown_workflow_key_is_refused():
    errors = refused(block(document(workflow__owner="me")))

    assert "workflow: unknown key owner" in errors


def test_an_unknown_top_level_key_is_refused():
    errors = refused(block(document(extra={"a": 1})))

    assert "file: unknown key extra" in errors


def test_an_unknown_next_outcome_is_refused():
    errors = refused(block(document(stages__build__next={"pass": "verdict", "fail": "build"})))

    assert "stages.build.next: unknown key fail" in errors


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("workflow__name", "workflow: missing required key name"),
        ("workflow__intent", "workflow: missing required key intent"),
        ("workflow__head", "workflow: missing required key head"),
        ("workflow__exit", "workflow: missing required key exit"),
        ("stages__build__profile", "stages.build: missing required key profile"),
    ],
)
def test_a_missing_required_key_is_refused(path, message):
    doc = copy.deepcopy(BASE)
    *parents, last = path.split("__")
    target = doc
    for part in parents:
        target = target[part]
    del target[last]

    assert message in refused(block(doc))


def test_a_next_target_naming_a_missing_stage_is_refused():
    errors = refused(block(document(stages__build__next={"pass": "review"})))

    assert "stages.build.next.pass: review is not a stage, exit or null" in errors


def test_a_head_that_is_not_a_stage_is_refused():
    errors = refused(block(document(workflow__head="scout")))

    assert "workflow.head: scout is not a stage" in errors


def test_an_input_naming_an_unknown_stage_is_refused():
    errors = refused(block(document(stages__build__input=["case", "plan"])))

    assert "stages.build.input: plan is not case or a stage" in errors


def test_an_exit_naming_an_unknown_stage_pass_is_refused():
    errors = refused(block(document(workflow__exit="review.pass")))

    assert "workflow.exit: review is not a stage" in errors


def test_free_text_exits_are_allowed():
    workflow = schema.parse(
        block(document(workflow__exit="the case is met", stages__build__exit="every point has evidence"))
    )

    assert workflow.exit == "the case is met"
    assert workflow.stages["build"].exit == "every point has evidence"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("workflow__on_exhausted", "retry", "workflow.on_exhausted: retry is not one of block, escalate, keep-best"),
        ("stages__build__tier", "mid", "stages.build.tier: mid is not one of high, low"),
        ("workflow__max_iterations", 0, "workflow.max_iterations: must be a positive integer"),
        ("stages__build__count", True, "stages.build.count: must be a positive integer"),
        ("stages__build__iterations", "2", "stages.build.iterations: must be a positive integer"),
        ("stages__build__filters", "judge-blind", "stages.build.filters: must be a list of names"),
        ("stages__build__profile", 7, "stages.build.profile: must be text"),
        ("stages__build__exit", 3, "stages.build.exit: must be text or null"),
    ],
)
def test_values_of_the_wrong_kind_are_refused(path, value, message):
    assert message in refused(block(document(**{path: value})))


@pytest.mark.parametrize(
    "text",
    [
        "workflow: {name: tiny}\nstages: {}\n",
        "workflow:\n  name: tiny\nstages:\n  build:\n    profile: engineer\n    input: [case]\n",
    ],
)
def test_yaml_flow_style_is_refused(text):
    assert "flow style" in refused(text)


def test_an_empty_flow_list_stays_allowed():
    text = block(document(stages__build__filters=[]))

    workflow = schema.parse(text)

    assert "filters: []" in text
    assert workflow.stages["build"].filters == ()


def test_text_that_is_not_a_mapping_is_refused():
    assert "a workflow file is a mapping" in refused("- one\n- two\n")


def test_text_that_is_not_yaml_is_refused():
    assert "not YAML" in refused("workflow:\n  name: [unclosed\n")


def test_every_error_is_reported_at_once():
    doc = document(stages__build__model="opus", workflow__head="scout")

    errors = refused(block(doc)).splitlines()

    assert len(errors) == 2
