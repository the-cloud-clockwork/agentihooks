import pytest

from hooks.classifier import Choice, ClassifierInputError, Score, YesNo
from hooks.classifier.questions import validate, wire_questions


def test_each_type_renders_its_wire_shape():
    questions = {
        "tier": Choice("Which tier?", {"small": "s", "large": "l"}),
        "effort": Score("How much?", ["low", "high"]),
        "trivial": YesNo("One line?", true="yes it is", false="no it is not"),
    }
    assert wire_questions(questions) == {
        "tier": {"type": "choice", "instructions": "Which tier?", "criteria": {"small": "s", "large": "l"}},
        "effort": {"type": "score", "instructions": "How much?", "criteria": ["low", "high"]},
        "trivial": {
            "type": "noul",
            "instructions": "One line?",
            "criteria": {"true": "yes it is", "false": "no it is not"},
        },
    }


def test_valid_questions_pass():
    validate({"a": YesNo("q", true="t", false="f"), "b_2": Choice("q", {"x": "x"})})


@pytest.mark.parametrize("name", ["", "Upper", "1lead", "has-dash", "a" * 65, "sp ace"])
def test_bad_question_name_is_refused(name):
    with pytest.raises(ClassifierInputError, match="name"):
        validate({name: YesNo("q", true="t", false="f")})


def test_name_of_sixty_four_characters_passes():
    validate({"a" * 64: YesNo("q", true="t", false="f")})


def test_no_questions_is_refused():
    with pytest.raises(ClassifierInputError, match="1 to 128"):
        validate({})


def test_more_than_128_questions_is_refused():
    many = {f"q{i}": YesNo("q", true="t", false="f") for i in range(129)}
    with pytest.raises(ClassifierInputError, match="1 to 128"):
        validate(many)
    validate(dict(list(many.items())[:128]))


def test_more_than_255_options_is_refused():
    options = {f"o{i}": "d" for i in range(256)}
    with pytest.raises(ClassifierInputError, match="255"):
        validate({"pick": Choice("q", options)})
    options.pop("o0")
    validate({"pick": Choice("q", options)})


def test_choice_without_options_is_refused():
    with pytest.raises(ClassifierInputError, match="255"):
        validate({"pick": Choice("q", {})})


def test_more_than_10_levels_is_refused():
    levels = [f"l{i}" for i in range(11)]
    with pytest.raises(ClassifierInputError, match="10"):
        validate({"lvl": Score("q", levels)})
    validate({"lvl": Score("q", levels[:10])})


def test_score_without_levels_is_refused():
    with pytest.raises(ClassifierInputError, match="10"):
        validate({"lvl": Score("q", [])})


def test_unknown_question_object_is_refused():
    with pytest.raises(ClassifierInputError, match="YesNo, Choice or Score"):
        validate({"x": {"type": "noul"}})
