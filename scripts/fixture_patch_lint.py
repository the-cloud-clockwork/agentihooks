"""Reject fixture mock patches overlapped by monkeypatch attribute overrides."""

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Function:
    path: Path
    name: str
    fixture: bool
    autouse: bool
    requires: tuple[str, ...]
    patches: tuple[tuple[str, int], ...]
    overrides: tuple[tuple[str, int], ...]


def qualified(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{qualified(node.value, aliases)}.{node.attr}"
    return ""


def imports(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def literal(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def target(call: ast.Call, aliases: dict[str, str], object_form: bool) -> str:
    if not call.args:
        return ""
    if not object_form:
        return literal(call.args[0])
    if len(call.args) < 2:
        return ""
    owner, attribute = qualified(call.args[0], aliases), literal(call.args[1])
    return f"{owner}.{attribute}" if owner and attribute else ""


def attributes(
    node: ast.AST, aliases: dict[str, str]
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]:
    patches: list[tuple[str, int]] = []
    overrides: list[tuple[str, int]] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        name = qualified(call.func, aliases)
        if name in {"unittest.mock.patch", "unittest.mock.patch.object"}:
            value = target(call, aliases, name.endswith(".object"))
            if value:
                patches.append((value, call.lineno))
        elif name == "monkeypatch.setattr":
            value = target(call, aliases, not isinstance(call.args[0], ast.Constant)) if call.args else ""
            if value:
                overrides.append((value, call.lineno))
    return tuple(patches), tuple(overrides)


def functions(path: Path, tree: ast.Module) -> list[Function]:
    aliases = imports(tree)
    found: list[Function] = []
    for node in ast.walk(tree):
        if not isinstance(node, FUNCTIONS):
            continue
        decorators = [
            d
            for d in node.decorator_list
            if qualified(d.func if isinstance(d, ast.Call) else d, aliases) == "pytest.fixture"
        ]
        autouse = any(
            isinstance(d, ast.Call)
            and any(
                k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value is True for k in d.keywords
            )
            for d in decorators
        )
        patches, overrides = attributes(node, aliases)
        found.append(
            Function(
                path, node.name, bool(decorators), autouse, tuple(a.arg for a in node.args.args), patches, overrides
            )
        )
    return found


def selected(test: Function, fixtures: dict[str, Function]) -> list[Function]:
    pending = [*test.requires, *(name for name, item in fixtures.items() if item.autouse)]
    seen: set[str] = set()
    found: list[Function] = []
    while pending:
        name = pending.pop()
        if name in seen or name not in fixtures:
            continue
        seen.add(name)
        fixture = fixtures[name]
        found.append(fixture)
        pending.extend(fixture.requires)
    return found


def overlaps(module: list[Function], parents: list[Function]) -> list[str]:
    fixtures = {item.name: item for item in [*parents, *module] if item.fixture}
    problems: set[str] = set()
    for test in module:
        if test.fixture or not test.name.startswith("test_"):
            continue
        active = selected(test, fixtures)
        for fixture in active:
            for patched, patch_line in fixture.patches:
                for owner in [test, *active]:
                    if (
                        owner.path != fixture.path
                        and owner.path.name == "conftest.py"
                        and fixture.path.is_relative_to(owner.path.parent)
                    ):
                        continue
                    for overridden, override_line in owner.overrides:
                        if patched == overridden:
                            problems.add(
                                f"{fixture.path}:{patch_line}: fixture {fixture.name} mock patch overlaps "
                                f"{owner.path}:{override_line}: {owner.name} monkeypatch on {patched}"
                            )
    return sorted(problems)


def discover(root: Path) -> tuple[list[str], int]:
    modules: dict[Path, list[Function]] = {}
    problems: list[str] = []
    paths = sorted((root / "tests").rglob("*.py"))
    for path in paths:
        relative = path.relative_to(root)
        try:
            tree = ast.parse(path.read_text(), filename=str(relative))
        except (SyntaxError, UnicodeError) as error:
            problems.append(f"{relative}: cannot parse: {error}")
            continue
        modules[relative] = functions(relative, tree)
    for path, module in modules.items():
        parents = [
            item for directory in reversed(path.parent.parents) for item in modules.get(directory / "conftest.py", [])
        ]
        if path.name != "conftest.py":
            parents.extend(modules.get(path.parent / "conftest.py", []))
            problems.extend(overlaps(module, parents))
    if not paths:
        problems.append("No Python test files found")
    return sorted(set(problems)), len(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    problems, count = discover(args.root)
    print(
        f"Fixture patch lint: {count} files, {len(problems)} overlaps or errors"
        if problems
        else f"Fixture patch lint: {count} files, 0 overlaps"
    )
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
