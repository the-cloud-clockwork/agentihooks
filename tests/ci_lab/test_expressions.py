import pytest

from scripts.ci_lab import expressions

CONTEXT = {
    "github.event_name": "pull_request",
    "inputs.base": "origin/dev",
    "steps.self-plant.outputs.changed": "true",
    "steps.empty.outputs.sha": "",
    "steps.empty.outputs.changed": "",
    "steps.count.outputs.one": " 1 ",
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
        ("steps.empty.outputs.sha", ""),
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
        ("'Pull_Request' == github.event_name", True),
        ("true == true", True),
        ("true == 'true'", False),
        ("'' == false", True),
        ("null == false", True),
        ("'0' == false", True),
        ("'  ' == false", True),
        ("steps.count.outputs.one == true", True),
        ("'1e0' == true", True),
        ("'1E+0' == true", True),
        ("'-0.0' == false", True),
        ("'+1' == true", False),
        ("'.0' == false", False),
        ("'1.' == true", False),
        ("'01' == true", False),
        ("'0x1' == true", False),
        ("'1.5e1' == '15'", False),
        ("'x' == false", False),
        ("'x' != false", True),
        ("'x' == 'x' == true", True),
        ("'a' == 'b' == false", True),
        ("!'x' == 'y'", False),
        ("inputs.base && 'yes'", "yes"),
        ("true && inputs.base", "origin/dev"),
        ("steps.empty.outputs.sha && 'yes'", ""),
        ("inputs.base && true && 'last'", "last"),
        ("inputs.base || 'other'", "origin/dev"),
        ("steps.empty.outputs.sha || inputs.base", "origin/dev"),
        ("'' || false || 'last'", "last"),
        ("true || true && false", True),
        ("!(true && false)", True),
        ("(inputs.base)", "origin/dev"),
        ("github.event_name == 'push' && 'origin/dev' || inputs.base", "origin/dev"),
        ("!cancelled() && steps.self-plant.outputs.changed == 'true'", True),
    ],
)
@pytest.mark.feature("lab-static-step-env")
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
        ("true && )", "expected a value, found ')'"),
        ("true || && true", "expected a value, found '&&'"),
        ("(true", "expected ')', found ''"),
        ("(true true", "expected ')', found 'true'"),
        ("true)", "expected the end, found ')'"),
        ("steps.missing.outputs.sha", "no value for 'steps.missing.outputs.sha' in the context"),
        ("inputs.base || steps.missing.outputs.sha", "no value for 'steps.missing.outputs.sha' in the context"),
        ("contains(inputs.base)", "no value for 'contains' in the context"),
        ("inputs.base (true)", "expected the end, found '('"),
        ("contains(inputs.base, 'dev')", "cannot read ','"),
        ("inputs.base < 'z'", "cannot read '<'"),
        ("1 == 1", "cannot read '1 1'"),
        ("'open", 'cannot read "\'"'),
    ],
)
@pytest.mark.feature("lab-static-step-env")
def test_syntax_outside_the_subset_is_refused(expression: str, message: str) -> None:
    with pytest.raises(ValueError) as refused:
        expressions.evaluate(expression, CONTEXT)
    assert str(refused.value) == message


def interpolate_replaces_each_embedded_expression_with_its_text() -> None:
    text = "swarm-${{ inputs.base }}-${{ true }}-${{ false }}-${{ steps.empty.outputs.sha }}-${{github.event_name}}"
    assert expressions.interpolate(text, CONTEXT) == "swarm-origin/dev-true-false--pull_request"
    assert expressions.interpolate(".semgrep", CONTEXT) == ".semgrep"
    assert expressions.interpolate("${{\n  inputs.base\n}}", CONTEXT) == "origin/dev"


@pytest.mark.parametrize(
    ("condition", "held"),
    [
        ("", True),
        ("   ", True),
        ("${{ !cancelled() }}", True),
        ("${{ cancelled() }}", False),
        ("  ${{ steps.self-plant.outputs.changed == 'true' }}\n", True),
        ("${{ steps.empty.outputs.changed == 'true' }}", False),
        ("${{\n  !cancelled() &&\n  steps.self-plant.outputs.changed == 'true'\n}}", True),
        ("github.event_name == 'push'", False),
        ("steps.empty.outputs.sha", False),
        ("inputs.base", True),
    ],
)
@pytest.mark.feature("lab-static-step-env")
def test_a_condition_holds_when_its_expression_is_truthy(condition: str, held: bool) -> None:
    assert expressions.holds(condition, CONTEXT) is held


@pytest.mark.parametrize(
    ("condition", "texts", "names"),
    [
        ("", (), set()),
        ("${{ !cancelled() && steps.pick.outputs.browser == 'true' }}", (), {"steps.pick.outputs.browser"}),
        ("steps.venv.outputs.cache-hit != 'true'", (), {"steps.venv.outputs.cache-hit"}),
        ("${{ always() || (true && null) }}", ("plain, text: here", "a ${{ false }} b"), set()),
        (
            "",
            ("swarm-${{ github.run_id }}-${{ github.run_attempt }}", "${{ github.head_ref || github.ref_name }}"),
            {"github.run_id", "github.run_attempt", "github.head_ref", "github.ref_name"},
        ),
        ("${{ contains(github.ref, 'dev') }}", ("${{ secrets.KEY }}",), {"contains", "github.ref", "secrets.KEY"}),
    ],
)
@pytest.mark.feature("lab-static-step-env")
def test_reads_names_every_context_name_an_if_condition_and_the_expressions_in_its_values_read(
    condition: str, texts: tuple[str, ...], names: set[str]
) -> None:
    assert expressions.reads(condition, texts) == names
