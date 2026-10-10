from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from hooks import config
from hooks.classifier import decision_log, definitions
from hooks.context import profile_chain
from hooks.filters import runner as filter_runner
from scripts.swarm import difficulty, grouping, model_pick, priority_sweep, profile_choice, slice_screen, trace_plan
from scripts.swarm_ledger import ledger_duplicates

from .test_ledger_definitions import _duplicates

OUT_OF_RANGE = "threshold {} must be between zero and one"


def _never(*args, **kwargs):
    pytest.fail("decided with a refused definition")


def _ledger_duplicate(environ):
    return ledger_duplicates.find(_duplicates(), "task", [{"title": "Publish the wheel to PyPI"}], judge=_never)


def _filter(environ):
    spec = SimpleNamespace(mode="classifier", intent_from=None, intent="no ids", question="Q?")
    findings = [filter_runner.Finding((), 0, 2, "t1", "an id")]
    try:
        return filter_runner.confirm({"file": "f.filter.yaml", "path": "f"}, spec, {"tool_input": {}}, findings)
    except definitions.DefinitionError:
        return "refused"


def _phase_slice(environ):
    phase = {"id": "p1", "title": "Build", "description": "Intent."}
    plan = {"id": "plan-p1", "phase": "p1", "kind": "plan", "lane": "plan", "proof": {"slice": "t1"}}
    task = {"id": "t1", "phase": "p1", "title": "One", "description": "Work."}
    return slice_screen.screen(phase, {"overview": "Ship.", "phases": [phase], "tasks": [plan, task]}, 0.7).reason


def _trace_plan(environ):
    return trace_plan.trace([trace_plan.Piece("piece", ("src",), "why")], {}, None)["verdict"]


def _task_grouping(environ):
    task = {"title": "One", "description": "Work.", "difficulty": "S", "territory": ["page"]}
    return grouping.confirm([[{**task, "id": "t1"}, {**task, "id": "t2"}]], {"overview": "Ship."})


def _task_difficulty(environ):
    return difficulty.classify({"id": "t1", "title": "One"}, {"phases": []})


def _profile_pick(environ):
    try:
        return profile_choice.classify("sw", {"id": "t1", "title": "One"}, environ)
    except profile_choice.ProfileUnresolved:
        return "refused"


def _model_pick(environ):
    return model_pick.pick("claude", {"model": "auto", "effort": "auto"}, {}, environ)


def _priority_resolve(environ):
    doc = {"tasks": [{"id": "t1", "title": "One"}]}
    write = priority_sweep.Write("operator", "comment", "tasks/t1", "Done.")
    return priority_sweep._judge(doc, {"item": "tasks/t1", "text": "Decide."}, write, _never)


CALLERS = [
    ("ledger-duplicate", "same", _ledger_duplicate, [ledger_duplicates.UNCHECKED]),
    ("filter", "confirm", _filter, "refused"),
    ("phase-slice", "off_intent", _phase_slice, "the classifier did not answer"),
    ("trace-plan", "off_intent", _trace_plan, trace_plan.UNCHECKED),
    ("task-grouping", "confidence", _task_grouping, None),
    ("task-difficulty", "confidence", _task_difficulty, difficulty.sized(difficulty.FALLBACK, "default", 0.0)),
    ("profile-pick", "confidence", _profile_pick, "refused"),
    ("model-pick", "confidence", _model_pick, model_pick.ModelPick("auto", "auto")),
    ("priority-resolve", "probability", _priority_resolve, None),
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path / "home")
    for module in (slice_screen, trace_plan, grouping, difficulty, profile_choice, model_pick):
        monkeypatch.setattr(module, "decide", _never)
    monkeypatch.setattr("hooks.classifier.decide", _never)
    monkeypatch.setattr(profile_choice, "state", lambda slug, task: {"task": task["id"]})
    monkeypatch.setattr(profile_choice, "_remedy", lambda slug, task: "set it by hand")
    return tmp_path


def _refusal(name):
    return [entry["failures"] for entry in decision_log.read(name)]


@pytest.mark.parametrize(("name", "key", "call", "fallback"), CALLERS)
def test_a_malformed_threshold_override_is_refused_logged_and_falls_back(home, monkeypatch, name, key, call, fallback):
    variable = f"AGENTIHOOKS_CLASSIFIER_{name.upper().replace('-', '_')}_{key.upper()}"
    monkeypatch.setenv(variable, "1.5")
    with patch.object(profile_chain, "read_state", return_value={}):
        assert call({variable: "1.5"}) == fallback
    assert _refusal(name) == [[{"model": "definition", "reason": OUT_OF_RANGE.format(key)}]]


@pytest.mark.parametrize(("name", "key", "call", "fallback"), CALLERS)
@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        pytest.param(
            lambda package, key: package.update(thresholds={key: 0.5, "extra": 0.5}),
            "overrides must preserve package threshold keys",
            id="threshold-keys",
        ),
        pytest.param(lambda package, key: package.update(version=2), "definition version must be 1", id="version"),
    ],
)
def test_a_malformed_bundle_definition_is_refused_logged_and_falls_back(home, name, key, call, fallback, fault, reason):
    package = yaml.safe_load((profile_chain.BUILT_IN_PROFILES / "package" / "classifiers" / f"{name}.yaml").read_text())
    fault(package, key)
    folder = home / "bundle" / ".claude" / "classifiers"
    folder.mkdir(parents=True)
    (folder / f"{name}.yaml").write_text(yaml.safe_dump(package))
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(home / "bundle")}}):
        assert call({}) == fallback
    assert _refusal(name) == [[{"model": "definition", "reason": reason}]]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("instructions", "{missing}", "cannot format classifier question: 'missing'"),
        ("key", "Finding{index}", "question name 'Finding0' must match ^[a-z][a-z0-9_]{0,63}$"),
    ],
)
def test_a_bundle_question_that_cannot_be_built_is_refused_and_logged(home, field, value, reason):
    package = yaml.safe_load((profile_chain.BUILT_IN_PROFILES / "package" / "classifiers" / "filter.yaml").read_text())
    package["questions"][0][field] = value
    folder = home / "bundle" / ".claude" / "classifiers"
    folder.mkdir(parents=True)
    (folder / "filter.yaml").write_text(yaml.safe_dump(package))
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(home / "bundle")}}):
        assert _filter({}) == "refused"
    assert _refusal("filter") == [[{"model": "definition", "reason": reason}]]
