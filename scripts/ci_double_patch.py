"""Refuse a test that monkeypatches a target an autouse fixture above it patches with mock.patch."""

import argparse
import ast
from pathlib import Path
from typing import NamedTuple

PATCH = {"unittest.mock.patch", "mock.patch"}
PATCH_OBJECT = {f"{name}.object" for name in PATCH}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


class Patch(NamedTuple):
    """One target patched at a line inside a named function of a file."""

    target: str
    path: Path
    line: int
    function: str


def qualified(node: ast.AST) -> str:
    """Return the dotted name an expression spells, or an empty string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := qualified(node.value)):
        return f"{base}.{node.attr}"
    return ""


def aliases_for(tree: ast.Module) -> dict[str, str]:
    """Map each imported name in a module to the dotted path it binds."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update((name.asname or name.name, name.name) for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            aliases.update((name.asname or name.name, f"{node.module}.{name.name}") for name in node.names)
    return aliases


def resolved(node: ast.AST, aliases: dict[str, str]) -> str:
    """Return the dotted import path of an expression through the module's aliases."""
    first, _, rest = qualified(node).partition(".")
    base = aliases.get(first, first)
    return f"{base}.{rest}" if rest else base


def attribute_target(args: list[ast.expr], aliases: dict[str, str]) -> str:
    """Return the dotted target of an owner and attribute name pair, or an empty string."""
    match args:
        case [owner_node, ast.Constant(value=str() as attribute), *_]:
            owner = resolved(owner_node, aliases)
            return f"{owner}.{attribute}" if owner else ""
        case _:
            return ""


def mock_target(call: ast.Call, aliases: dict[str, str]) -> str:
    """Return the target a mock.patch or patch.object call replaces, or an empty string."""
    name = resolved(call.func, aliases)
    match call.args:
        case [ast.Constant(value=str() as target), *_] if name in PATCH:
            return target
        case _:
            return attribute_target(call.args, aliases) if name in PATCH_OBJECT else ""


def monkeypatch_target(call: ast.Call, aliases: dict[str, str]) -> str:
    """Return the target a monkeypatch.setattr call replaces, or an empty string."""
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "setattr":
        return ""
    if qualified(call.func.value) != "monkeypatch":
        return ""
    match call.args:
        case [ast.Constant(value=str() as target), *_]:
            return target
        case _:
            return attribute_target(call.args, aliases)


def functions(scope: ast.Module | ast.ClassDef, prefix: str = "") -> list[tuple[str, FunctionNode]]:
    """List the module and class level functions of a scope with their dotted names."""
    found: list[tuple[str, FunctionNode]] = []
    for node in scope.body:
        if isinstance(node, ast.ClassDef):
            found += functions(node, f"{prefix}{node.name}.")
        elif isinstance(node, FUNCTIONS):
            found.append((f"{prefix}{node.name}", node))
    return found


def autouse(function: FunctionNode) -> bool:
    """Tell whether a function is declared a pytest fixture with autouse true."""
    return any(
        isinstance(decorator, ast.Call)
        and qualified(decorator.func).rpartition(".")[2] == "fixture"
        and any(
            keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in function.decorator_list
    )


def patches(tree: ast.Module, path: Path) -> tuple[list[Patch], list[Patch]]:
    """Return the mock patches of a module's autouse fixtures and every monkeypatch of the module."""
    aliases = aliases_for(tree)
    mocked: list[Patch] = []
    monkeyed: list[Patch] = []
    for name, function in functions(tree):
        fixture = autouse(function)
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            if fixture and (target := mock_target(call, aliases)):
                mocked.append(Patch(target, path, call.lineno, name))
            if target := monkeypatch_target(call, aliases):
                monkeyed.append(Patch(target, path, call.lineno, name))
    return mocked, monkeyed


def reaching(path: Path, root: Path) -> list[Path]:
    """List the conftest files whose fixtures reach a file, farthest first, then the file itself."""
    folders = [folder for folder in path.parents if folder == root or root in folder.parents]
    return [*(folder / "conftest.py" for folder in reversed(folders)), path]


def main(argv: list[str] | None = None) -> int:
    """Grade every Python file under the tests folder and report each double patch."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    files = sorted((args.root / "tests").rglob("*.py"))
    problems: list[str] = []
    parsed: dict[Path, tuple[list[Patch], list[Patch]]] = {}
    if not files:
        problems.append("No Python test files found")
    top = args.root / "conftest.py"
    for path in [*files, top] if top.is_file() else files:
        try:
            parsed[path] = patches(ast.parse(path.read_text()), path)
        except (SyntaxError, UnicodeError) as error:
            problems.append(f"{path.relative_to(args.root)}: cannot parse: {error}")
    for path in files:
        mocked = {
            patch.target: patch for source in reaching(path, args.root) for patch in parsed.get(source, ([], []))[0]
        }
        for patch in parsed.get(path, ([], []))[1]:
            if fixture := mocked.get(patch.target):
                problems.append(
                    f"{patch.path.relative_to(args.root)}:{patch.line}: {patch.function} monkeypatches {patch.target}, "
                    f"which autouse fixture {fixture.function} patches with mock.patch at "
                    f"{fixture.path.relative_to(args.root)}:{fixture.line}; patch it through monkeypatch in that "
                    "fixture so one stack unwinds both"
                )
    print(f"Double patches: {len(files)} files, {len(problems)} problems")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
