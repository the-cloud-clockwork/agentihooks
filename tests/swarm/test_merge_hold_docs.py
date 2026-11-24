import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/pillars/swarm.md"
SOURCE = ROOT / "scripts/swarm/ready.py"


def _templates(expression: ast.expr, constants: dict[str, str], functions: dict[str, ast.FunctionDef]) -> set[str]:
    match expression:
        case ast.Constant(value=str() as value):
            return {value}
        case ast.Name(id=name):
            return {constants[name]}
        case ast.Call(func=ast.Attribute(value=value, attr="format")):
            return _templates(value, constants, functions)
        case ast.Call(func=ast.Name(id=name)):
            returns = [node.value for node in ast.walk(functions[name]) if isinstance(node, ast.Return)]
            assert returns and all(value is not None for value in returns), f"No reason returned by {name}"
            return set().union(*(_templates(value, constants, functions) for value in returns if value is not None))
        case _:
            raise AssertionError(f"Unsupported hold reason expression: {ast.dump(expression)}")


def _written_reasons(source: str) -> set[str]:
    tree = ast.parse(source)
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    reasons = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_hold":
            reasons.update(_templates(node.args[3], constants, functions))
    assert reasons, "No merge pass hold reasons found"
    return {_normalize(reason) for reason in reasons}


def _normalize(reason: str) -> str:
    return re.sub(r"\{[^{}]+\}|<[^<>]+>", "{}", reason)


def _documented_reasons(page: str) -> set[str]:
    section = page.split("#### Merge pass hold lines\n", 1)[1].split("\n#### ", 1)[0]
    table = section.split("| Reason | Written when | What to do |\n|---|---|---|\n", 1)[1].split("\n\n", 1)[0]
    reasons = {row.split("|")[1].strip().strip("`") for row in table.splitlines()}
    assert reasons, "No documented merge pass hold reasons found"
    return {_normalize(reason) for reason in reasons}


def _assert_reasons_match(source: str, page: str) -> None:
    written = _written_reasons(source)
    documented = _documented_reasons(page)
    assert written == documented, f"Missing docs: {written - documented}; stale docs: {documented - written}"


@pytest.mark.feature("merge-pass-hold-reason-docs")
def test_the_hold_reason_table_matches_the_reasons_the_merge_pass_writes() -> None:
    _assert_reasons_match(SOURCE.read_text(), PAGE.read_text())


@pytest.mark.feature("merge-pass-hold-reason-docs")
def test_every_missing_hold_reason_row_fails() -> None:
    page = PAGE.read_text()
    source = SOURCE.read_text()
    table = page.split("| Reason | Written when | What to do |\n|---|---|---|\n", 1)[1].split("\n\n", 1)[0]
    for row in table.splitlines():
        with pytest.raises(AssertionError, match="Missing docs:"):
            _assert_reasons_match(source, page.replace(row + "\n", "", 1))


@pytest.mark.feature("merge-pass-hold-reason-docs")
@pytest.mark.parametrize("reason_cell", ["`a removed reason`", "a removed reason"])
def test_a_stale_hold_reason_row_fails(reason_cell: str) -> None:
    page = PAGE.read_text().replace(
        "| Reason | Written when | What to do |\n|---|---|---|\n",
        f"| Reason | Written when | What to do |\n|---|---|---|\n| {reason_cell} | Removed. | None. |\n",
        1,
    )
    with pytest.raises(AssertionError, match="stale docs:"):
        _assert_reasons_match(SOURCE.read_text(), page)
