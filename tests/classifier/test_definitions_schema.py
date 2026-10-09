import hashlib
import json

import pytest

from hooks.classifier.definitions import DefinitionError, load
from hooks.classifier.questions import Choice, Score, YesNo

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", None, "definition version must be 1"),
        ("version", True, "definition version must be 1"),
        ("version", 2, "definition version must be 1"),
        ("purpose", " ", "purpose must be nonempty text"),
        ("purpose", 5, "purpose must be nonempty text"),
        ("fallbacks", "other", "fallbacks must be cli or none"),
        ("questions", {}, "questions must be a list"),
        ("thresholds", [], "thresholds must be a mapping"),
        ("thresholds", {"yes": True}, "threshold yes must be between zero and one"),
        ("thresholds", {"yes": "0.6"}, "threshold yes must be between zero and one"),
        ("thresholds", {"yes": -0.1}, "threshold yes must be between zero and one"),
        ("thresholds", {"yes": float("inf")}, "threshold yes must be between zero and one"),
        ("thresholds", {"yes": float("nan")}, "threshold yes must be between zero and one"),
        ("thresholds", {"bad/key": 0.6}, "invalid threshold key: bad/key"),
        ("rule", [], "rule must be a mapping"),
        ("rule", {"type": "other"}, "unknown verdict rule: other"),
        ("rule", {"type": "yes"}, "yes rule requires a threshold key"),
        ("rule", {"type": "yes", "threshold": "missing"}, "rule threshold must name a defined threshold"),
        ("rule", {"type": "yes", "threshold": []}, "rule threshold must name a defined threshold"),
        ("rule", {"type": "choice"}, "rule choice does not match its questions"),
        ("rule", {"type": "yes", "extra": 1}, "unknown rule keys: extra"),
        ("unknown", 1, "unknown definition keys: unknown"),
    ],
)
def test_schema_refuses_invalid_fields(definition_home, field, value, message):
    raw = sample()
    raw[field] = value
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == message


@pytest.mark.parametrize("raw", [None, [], "text"])
def test_definition_requires_mapping(definition_home, raw):
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == "definition must be a mapping"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("instructions", "", "instructions must be nonempty text"),
        ("true", "", "true must be nonempty text"),
        ("false", 7, "false must be nonempty text"),
        ("each", "bad/key", "invalid each parameter: bad/key"),
        ("each", False, "each parameter must be nonempty text"),
        ("unknown", 1, "unknown question keys: unknown"),
    ],
)
def test_question_fields_are_validated(definition_home, field, value, message):
    raw = sample()
    raw["questions"][0][field] = value
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == message


@pytest.mark.parametrize(
    ("kind", "field", "value", "message"),
    [
        ("choice", "options", [], "choice options must be a nonempty mapping"),
        ("choice", "options", {}, "choice options must be a nonempty mapping"),
        ("choice", "options", {"": "text"}, "option must be nonempty text"),
        ("choice", "options", {"a": ""}, "option text must be nonempty text"),
        ("score", "levels", {}, "score levels must be a nonempty list"),
        ("score", "levels", [], "score levels must be a nonempty list"),
        ("score", "levels", [""], "level must be nonempty text"),
    ],
)
def test_answer_options_are_validated(definition_home, kind, field, value, message):
    raw = sample()
    raw["questions"][0].update(type=kind, **{field: value})
    raw["rule"] = {"type": kind}
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == message


def test_duplicate_question_keys_are_refused(definition_home):
    raw = sample()
    raw["questions"].append(dict(raw["questions"][0]))
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == "question names must be unique"


@pytest.mark.parametrize("name", ["../sample", "UPPER", "", " "])
def test_definition_names_cannot_escape_the_registry(definition_home, name):
    with pytest.raises(DefinitionError):
        load(name)


def test_missing_definition_is_named(definition_home):
    with pytest.raises(DefinitionError) as error:
        load("missing")
    assert str(error.value) == "unknown classifier definition: missing"


@pytest.mark.parametrize(
    ("kind", "extra", "question"),
    [
        ("yesno", {"true": "Accepted", "false": "Rejected"}, YesNo("Accept?", "Accepted", "Rejected")),
        ("choice", {"options": {"a": "Alpha", "b": "Beta"}}, Choice("Accept?", {"a": "Alpha", "b": "Beta"})),
        ("score", {"levels": ["low", "high"]}, Score("Accept?", ["low", "high"])),
    ],
)
def test_loaded_questions_retain_typed_contract(definition_home, kind, extra, question):
    raw = sample()
    raw["questions"][0].update(type=kind, each="items", **extra)
    raw["rule"] = {"type": "code"}
    del raw["fallbacks"]
    write_definition(definition_home, raw)
    loaded = load("sample")
    assert loaded.name == "sample"
    assert loaded.purpose == "sample"
    assert loaded.fallbacks == "cli"
    assert loaded.questions[0].name == "accept"
    assert loaded.questions[0].question == question
    assert loaded.questions[0].each == "items"
    assert loaded.rule.type == "code"
    assert loaded.rule.threshold is None
    assert len(loaded.digest) == 64
    assert loaded.digest == load("sample").digest


def test_digest_is_sha256_of_the_effective_definition(definition_home):
    write_definition(definition_home, sample())
    canonical = {
        "name": "sample",
        "purpose": "sample",
        "fallbacks": "none",
        "questions": [
            {
                "name": "accept",
                "question": {"instructions": "Accept?", "true": "Yes", "false": "No", "type": "noul"},
                "each": None,
            }
        ],
        "thresholds": {"yes": 0.6},
        "rule": {"type": "yes", "threshold": "yes"},
        "digest": "",
    }
    expected = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    assert load("sample").digest == expected
