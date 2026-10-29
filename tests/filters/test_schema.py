import pytest

from hooks.filters import schema
from hooks.filters.schema import FilterSchemaError

pytestmark = pytest.mark.unit


def test_an_empty_filter_takes_every_default(tmp_path):
    path = tmp_path / "pre-write-x.filter.yaml"
    path.write_text("")
    spec = schema.load(path)
    assert spec == schema.FilterSpec()
    assert (spec.paths, spec.finders, spec.mode, spec.intent, spec.intent_from) == ((), (), "both", "", "")
    assert (spec.question, spec.action, spec.max_rounds) == (
        "Does this finding go against the filter intent?",
        "send-back",
        3,
    )


def test_every_key_is_read():
    spec = schema.parse(
        {
            "paths": ["*.py"],
            "finders": [{"regex": "a+", "reason": "too many a"}, {"regex": "b"}],
            "mode": "finders",
            "intent": "no a",
            "intent_from": "goal",
            "question": "Is it bad?",
            "action": "strip",
            "max_rounds": 5,
        }
    )
    assert spec.paths == ("*.py",)
    assert [(f.pattern.pattern, f.reason) for f in spec.finders] == [("a+", "too many a"), ("b", "matched a finder")]
    assert (spec.mode, spec.intent, spec.intent_from, spec.question, spec.action, spec.max_rounds) == (
        "finders",
        "no a",
        "goal",
        "Is it bad?",
        "strip",
        5,
    )


@pytest.mark.parametrize(
    "raw, message",
    [
        ([1], "a filter must be a mapping of keys"),
        ({"colour": 1, "action": "flag", "size": 2}, "unknown keys: colour, size"),
        ({"paths": "*.py"}, "paths must be a list of non-empty strings"),
        ({"paths": ["*.py", ""]}, "paths must be a list of non-empty strings"),
        ({"paths": [3]}, "paths must be a list of non-empty strings"),
        ({"finders": {"regex": "a"}}, "finders must be a list"),
        ({"finders": ["a"]}, "finder 0 must be a mapping with exactly one regex or script"),
        ({"finders": [{"reason": "x"}]}, "finder 0 must be a mapping with exactly one regex or script"),
        (
            {"finders": [{"regex": "a"}, {"regex": "a", "script": "x", "name": "y"}]},
            "finder 1 must be a mapping with exactly one regex or script",
        ),
        (
            {"finders": [{"regex": "("}]},
            "finder 0 regex does not compile: missing ), unterminated subpattern at position 0",
        ),
        ({"finders": [{"regex": 3}]}, "finder 0 regex must be text"),
        ({"finders": [{"regex": "a", "reason": 3}]}, "finder 0 reason must be text"),
        ({"mode": "all"}, "mode must be one of finders, classifier, both, got 'all'"),
        ({"intent": 3}, "intent must be text"),
        ({"intent_from": None}, "intent_from must be text"),
        ({"question": ["a"]}, "question must be text"),
        ({"action": "drop"}, "action must be one of send-back, strip, flag, got 'drop'"),
        ({"max_rounds": 0}, "max_rounds must be a whole number of at least 1, got 0"),
        ({"max_rounds": True}, "max_rounds must be a whole number of at least 1, got True"),
        ({"max_rounds": "3"}, "max_rounds must be a whole number of at least 1, got '3'"),
    ],
)
def test_an_invalid_filter_names_what_is_wrong(raw, message):
    with pytest.raises(FilterSchemaError) as refused:
        schema.parse(raw)
    assert str(refused.value) == message


@pytest.mark.parametrize("name", ["", "../other", "folder/name", "file.py", 1])
def test_invalid_script_names_are_rejected(name):
    message = "finder 0 script must be text" if isinstance(name, int) else "finder 0 script must be a finder name"
    with pytest.raises(FilterSchemaError) as error:
        schema.parse({"finders": [{"script": name}]})
    assert str(error.value) == message


def test_named_script_rejects_unknown_keys():
    with pytest.raises(FilterSchemaError, match="unknown keys: name"):
        schema.parse({"finders": [{"script": "string_literals", "name": "other"}]})


@pytest.mark.parametrize("name", ["string_literals", "My_finder-1"])
def test_named_finder_schema_keeps_the_script_name_and_reason(name):
    finder = schema.parse({"finders": [{"script": name, "reason": "custom reason"}]}).finders[0]
    assert finder.script == name
    assert finder.reason == "custom reason"
    assert finder.pattern is None


def test_one_round_is_allowed():
    assert schema.parse({"max_rounds": 1}).max_rounds == 1


def test_a_file_that_is_not_yaml_is_a_schema_error(tmp_path):
    path = tmp_path / "pre-write-x.filter.yaml"
    path.write_text("finders: [\n")
    with pytest.raises(FilterSchemaError) as refused:
        schema.load(path)
    assert str(refused.value).startswith("cannot read the filter: ")


def test_a_missing_file_is_a_schema_error(tmp_path):
    with pytest.raises(FilterSchemaError) as refused:
        schema.load(tmp_path / "gone.filter.yaml")
    assert str(refused.value).startswith("cannot read the filter: ")
