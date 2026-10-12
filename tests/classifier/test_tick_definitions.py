from unittest.mock import patch

import pytest

from hooks import config
from hooks.classifier import definitions, runner
from hooks.classifier.questions import Choice, YesNo
from hooks.context import profile_chain

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition

DIFFICULTY = {
    "S": "A mundane frontend change: copy, layout, style or a small control on a page, even when grouped with others.",
    "M": "Normal agentihooks code or CI work: hooks, scripts, commands, the ledger server, tests or workflows.",
    "L": (
        "Touches infrastructure, collects information, sets configuration or deploys to Kubernetes, possibly "
        "together with code."
    ),
}
RESPONSIBILITY = {
    "frontend": (
        "A product interface behavior: what a person sees or does on a page, panel, control or layout, including a "
        "product interaction such as claim ordering or ranking from a view, even when Python code implements it."
    ),
    "engineer": (
        "Backend or infrastructure behavior: services, APIs, command lines, hooks, runtimes, storage, deployment or "
        "CI plumbing, with no change to what a person sees or does in a product interface."
    ),
    "qa": (
        "Independent verification: stress testing or proving work that others built from evidence that needs no "
        "push. A qa seat never edits code, pushes a branch or opens a pull request, so it cannot run a CI probe."
    ),
    "split": "Two or more unrelated public responsibilities bundled in one task that should become separate tasks.",
    "unresolved": "The task does not say enough about the public behavior it changes to decide.",
}
CASES = [
    (
        "task-grouping",
        {"groups": ["t1 titled One, t2 titled Two", "t3 titled Three, t4 titled Four"]},
        {"confidence": 0.6},
        {
            f"group_{index}": YesNo(
                f"Tasks {named}: is this one change surface that one pull request, one review and one browser check "
                "cover?",
                "one change surface, one review and one browser check cover every task",
                "the tasks need separate changes, reviews or browser checks",
            )
            for index, named in enumerate(["t1 titled One, t2 titled Two", "t3 titled Three, t4 titled Four"])
        },
    ),
    (
        "task-difficulty",
        {"id": "t2", "title": 'Fix {the} "page"'},
        {"confidence": 0.6},
        {
            "difficulty": Choice(
                'Task t2 "Fix {the} "page"": how big is this task by the operator rubric? Judge the work it asks for '
                "from the description, kind, profile, territory and phase intent.",
                DIFFICULTY,
            )
        },
    ),
    (
        "profile-pick",
        {"id": None, "title": ""},
        {"confidence": 0.6},
        {
            "responsibility": Choice(
                'Task None "": which responsibility owns the public behavior this task changes? Judge what a user or '
                "caller observes changing, from the description, parent intent and territory, never from keywords or "
                "file names. Mixed interface and backend work follows the public behavior changed.",
                RESPONSIBILITY,
            )
        },
    ),
    (
        "model-pick",
        {"difficulty": "L", "rank": "urgent", "lower": "first option", "higher": "second option"},
        {"confidence": 0.6},
        {
            "option": Choice(
                "A coding agent will work this task. Its size is L (S small, M medium, L large) and its rank is "
                "urgent. Which run option lets the agent finish the task well while spending the least? Choose the "
                "option whose description fits the work the task asks for.",
                {"lower": "first option", "higher": "second option"},
            )
        },
    ),
    (
        "priority-resolve",
        {"kind": "comment", "subject": "Pick a port", "priority": "Which port?"},
        {"probability": 0.5},
        {
            "resolves": YesNo(
                "Does this comment on Pick a port resolve what its priority asks: Which port?",
                "the write resolves what the priority asks",
                "the priority still waits",
            )
        },
    ),
]


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    with patch.object(profile_chain, "read_state", return_value={}):
        yield


@pytest.mark.parametrize(("name", "params", "thresholds", "questions"), CASES)
def test_tick_definitions_reproduce_the_call_site_prompts(packaged, name, params, thresholds, questions):
    definition = definitions.load(name, environ={})
    rule = ("choice", "confidence") if name in ("profile-pick", "model-pick") else ("code", None)
    assert (definition.purpose, definition.fallbacks, definition.rule.type, definition.rule.threshold) == (
        name,
        "cli",
        *rule,
    )
    assert definition.thresholds == thresholds
    assert runner.questions_for(definition, params) == questions


@pytest.mark.parametrize(
    ("name", "legacy"),
    [
        ("profile-pick", "AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE"),
        ("model-pick", "AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE"),
    ],
)
def test_legacy_threshold_variables_yield_to_the_classifier_variable(packaged, name, legacy):
    current = f"AGENTIHOOKS_CLASSIFIER_{name.upper().replace('-', '_')}_CONFIDENCE"
    assert definitions.load(name, environ={legacy: "0.75"}).thresholds == {"confidence": 0.75}
    assert definitions.load(name, environ={legacy: "0.75", current: "0.7"}).thresholds == {"confidence": 0.7}
    assert definitions.load(name, environ={current: "0.65"}).thresholds == {"confidence": 0.65}
    for malformed in ("1.5", "nan", "much"):
        with pytest.raises(definitions.DefinitionError) as error:
            definitions.load(name, environ={legacy: malformed})
        assert str(error.value) == "threshold confidence must be between zero and one"
    with pytest.raises(definitions.DefinitionError, match="threshold confidence must be between zero and one"):
        definitions.load(name, environ={legacy: "0.5", current: "1.5"})


def test_definition_environment_names_its_legacy_variables(definition_home):
    raw = sample()
    raw["environment"] = {"yes": "OLD_SAMPLE_FLOOR"}
    write_definition(definition_home, raw)
    definition = definitions.load("sample", environ={"OLD_SAMPLE_FLOOR": "0.9"})
    assert (definition.thresholds, definition.environment) == ({"yes": 0.9}, {"yes": "OLD_SAMPLE_FLOOR"})
    assert definitions.load("sample", environ={}).environment == {"yes": "OLD_SAMPLE_FLOOR"}


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"no": "OLD_SAMPLE_FLOOR"}, "environment key no must name a defined threshold"),
        ({"yes": "old floor"}, "invalid environment variable: old floor"),
        ({"yes": 7}, "environment variable must be nonempty text"),
        (["yes"], "environment must be a mapping"),
    ],
)
def test_definition_environment_is_validated(definition_home, environment, message):
    raw = sample()
    raw["environment"] = environment
    write_definition(definition_home, raw)
    with pytest.raises(definitions.DefinitionError) as refused:
        definitions.load("sample", environ={})
    assert str(refused.value) == message
