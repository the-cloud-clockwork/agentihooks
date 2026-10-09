import pytest

from hooks.classifier import definitions, runner
from hooks.classifier.questions import Score, YesNo

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


def _load(folder, questions, rule="code"):
    raw = sample()
    raw["questions"] = questions
    raw["rule"] = {"type": rule}
    write_definition(folder, raw)
    return definitions.load("sample", environ={})


def test_a_key_template_names_each_expanded_question(definition_home):
    definition = _load(
        definition_home,
        [
            {
                "name": "same",
                "type": "yesno",
                "each": "pairs",
                "key": "new_{item[new]}_at_{index}",
                "instructions": "{x}",
            }
        ],
    )
    questions = runner.questions_for(definition, {"x": "Same?", "pairs": [{"new": 3}, {"new": 5}]})
    assert questions == {"new_3_at_0": YesNo("Same?"), "new_5_at_1": YesNo("Same?")}


def test_a_key_template_names_an_unexpanded_question_from_the_parameters(definition_home):
    definition = _load(definition_home, [{"name": "size", "type": "yesno", "key": "size_{n}", "instructions": "Big?"}])
    assert list(runner.questions_for(definition, {"n": 7})) == ["size_7"]


def test_questions_over_the_same_list_expand_element_by_element(definition_home):
    definition = _load(
        definition_home,
        [
            {"name": "size", "type": "score", "each": "tasks", "instructions": "Size {item}?", "levels": ["a", "b"]},
            {"name": "head", "type": "yesno", "instructions": "Head?"},
            {"name": "serves", "type": "yesno", "each": "tasks", "instructions": "Serves {item}?"},
            {"name": "other", "type": "yesno", "each": "more", "instructions": "Other {item}?"},
        ],
    )
    questions = runner.questions_for(definition, {"tasks": ["t1", "t2"], "more": ["m1"]})
    assert list(questions) == ["size_0", "serves_0", "size_1", "serves_1", "head", "other_0"]
    assert questions["serves_1"] == YesNo("Serves t2?")
    assert questions["size_0"] == Score("Size t1?", ["a", "b"])


def test_an_empty_list_asks_none_of_its_questions(definition_home):
    definition = _load(
        definition_home,
        [
            {"name": "piece", "type": "yesno", "each": "pieces", "instructions": "Need {item}?"},
            {"name": "size", "type": "yesno", "each": "sized", "key": "size", "instructions": "Big?"},
        ],
    )
    assert list(runner.questions_for(definition, {"pieces": ["a"], "sized": []})) == ["piece_0"]
    assert list(runner.questions_for(definition, {"pieces": ["a"], "sized": [{}]})) == ["piece_0", "size"]


def test_two_expansions_that_produce_the_same_name_are_refused(definition_home):
    definition = _load(
        definition_home, [{"name": "same", "type": "yesno", "each": "pairs", "key": "fixed", "instructions": "Same?"}]
    )
    with pytest.raises(definitions.DefinitionError, match="expanded question names must be unique"):
        runner.questions_for(definition, {"pairs": [1, 2]})


@pytest.mark.parametrize(
    ("key", "message"), [("", "question key must be nonempty text"), (3, "question key must be nonempty text")]
)
def test_a_key_must_be_text(definition_home, key, message):
    raw = sample()
    raw["questions"][0]["key"] = key
    write_definition(definition_home, raw)
    with pytest.raises(definitions.DefinitionError) as error:
        definitions.load("sample", environ={})
    assert str(error.value) == message


def test_a_loaded_key_is_kept_on_the_question_spec(definition_home):
    definition = _load(definition_home, [{"name": "a", "type": "yesno", "key": "a_{n}", "instructions": "A?"}])
    assert definition.questions[0].key == "a_{n}"
    assert _load(definition_home, [{"name": "a", "type": "yesno", "instructions": "A?"}]).questions[0].key is None
