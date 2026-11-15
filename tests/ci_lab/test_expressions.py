import pytest

from scripts.ci_lab import expressions

CONTEXT = {
    "github.event_name": "pull_request",
    "inputs.base": "origin/dev",
    "steps.self-plant.outputs.changed": "true",
}


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        ("'it''s'", "it's"),
        ("true", True),
        ("false", False),
        ("null", ""),
        ("inputs.base", "origin/dev"),
        ("  inputs.base  ", "origin/dev"),
        ("steps.missing.outputs.sha", ""),
        ("always()", True),
        ("success()", True),
        ("cancelled()", False),
        ("failure()", False),
        ("!cancelled()", True),
        ("!inputs.base", False),
        ("!!inputs.base", True),
        ("github.event_name == 'PULL_REQUEST'", True),
        ("github.event_name == 'push'", False),
        ("github.event_name != 'push'", True),
        ("github.event_name != 'pull_request'", False),
        ("true == true", True),
        ("true == 'true'", False),
        ("!'x' == 'y'", False),
        ("inputs.base && 'yes'", "yes"),
        ("steps.missing.outputs.sha && 'yes'", ""),
        ("inputs.base && true && 'last'", "last"),
        ("inputs.base || 'other'", "origin/dev"),
        ("steps.missing.outputs.sha || inputs.base", "origin/dev"),
        ("'' || false || 'last'", "last"),
        ("true || true && false", True),
        ("!(true && false)", True),
        ("(inputs.base)", "origin/dev"),
        ("github.event_name == 'push' && 'origin/dev' || inputs.base", "origin/dev"),
        ("!cancelled() && steps.self-plant.outputs.changed == 'true'", True),
    ],
)
def test_an_expression_evaluates_as_github_actions_evaluates_it(expression: str, value: str | bool) -> None:
    assert expressions.evaluate(expression, CONTEXT) == value
    assert type(expressions.evaluate(expression, CONTEXT)) is type(value)


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("", "expected a value, found ''"),
        ("true &&", "expected a value, found ''"),
        ("hashFiles()", "expected a value, found 'hashFiles()'"),
        ("== true", "expected a value, found '=='"),
        ("(true", "expected ')', found ''"),
        ("(true true", "expected ')', found 'true'"),
        ("true)", "expected the end, found ')'"),
        ("true == true == true", "expected the end, found '=='"),
        ("contains(inputs.base)", "expected the end, found '('"),
        ("contains(inputs.base, 'dev')", "cannot read \", 'dev')\""),
        ("inputs.base < 'z'", "cannot read \"< 'z'\""),
        ("1 == 1", "cannot read '1 == 1'"),
        ("'open", 'cannot read "\'open"'),
    ],
)
def test_syntax_outside_the_subset_is_refused(expression: str, message: str) -> None:
    with pytest.raises(ValueError) as refused:
        expressions.evaluate(expression, CONTEXT)
    assert str(refused.value) == message


def test_interpolate_replaces_each_embedded_expression_with_its_text() -> None:
    text = "swarm-${{ inputs.base }}-${{ true }}-${{ false }}-${{ steps.missing.outputs.sha }}-${{github.event_name}}"
    assert expressions.interpolate(text, CONTEXT) == "swarm-origin/dev-true-false--pull_request"
    assert expressions.interpolate(".semgrep", CONTEXT) == ".semgrep"


@pytest.mark.parametrize(
    ("condition", "held"),
    [
        ("", True),
        ("   ", True),
        ("${{ !cancelled() }}", True),
        ("${{ cancelled() }}", False),
        ("  ${{ steps.self-plant.outputs.changed == 'true' }}\n", True),
        ("${{ steps.missing.outputs.changed == 'true' }}", False),
        ("github.event_name == 'push'", False),
        ("steps.missing.outputs.sha", False),
        ("inputs.base", True),
    ],
)
def test_a_condition_holds_when_its_expression_is_truthy(condition: str, held: bool) -> None:
    assert expressions.holds(condition, CONTEXT) is held
