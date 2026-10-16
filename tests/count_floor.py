import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Runs inside each tree so the base imports its own sources, never the head's.
_COLLECT = """
import json, sys, pytest

class Collected:
    nodeids = []

    def pytest_collection_finish(self, session):
        Collected.nodeids = [item.nodeid for item in session.items]

code = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", sys.argv[1]], plugins=[Collected()])
with open(sys.argv[2], "w") as out:
    json.dump(Collected.nodeids if code == 0 else None, out)
"""


def collect_all(trees: dict[str, Path], tests: str) -> dict[str, list[str] | None]:
    with tempfile.TemporaryDirectory() as scratch:
        outputs = {side: Path(scratch) / f"{side}.json" for side in trees}
        runs = {
            side: subprocess.Popen(
                [sys.executable, "-c", _COLLECT, tests, str(outputs[side])], cwd=tree, stdout=subprocess.DEVNULL
            )
            for side, tree in trees.items()
        }
        for run in runs.values():
            run.wait()
        return {side: json.loads(outputs[side].read_text()) if outputs[side].is_file() else None for side in trees}


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _definitions(body: list[ast.stmt]) -> Iterator[str]:
    for node in body:
        if isinstance(node, _DEFINITIONS):
            yield node.name
        elif isinstance(node, (ast.If, ast.Try)):
            yield from _definitions(node.body + node.orelse + getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                yield from _definitions(handler.body)


def _module_name(relative: Path) -> str:
    parts = relative.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _sources(tree: Path, skipped: set[Path]) -> Iterator[Path]:
    for root, dirs, files in os.walk(tree):
        dirs[:] = [name for name in dirs if name != ".git" and (Path(root) / name).resolve() not in skipped]
        yield from (Path(root) / name for name in files if name.endswith(".py"))


def top_level_definitions(tree: Path, tests: str) -> set[tuple[str, str]]:
    definitions = set()
    for path in _sources(tree, {(tree / tests).resolve()}):
        try:
            body = ast.parse(path.read_bytes()).body
        except (OSError, SyntaxError, ValueError):
            continue
        module = _module_name(path.relative_to(tree))
        definitions.update((module, definition) for definition in _definitions(body))
    return definitions


def bound_names(tree: Path) -> tuple[set[str], list[Path]]:
    names, opaque = set(), []
    for path in _sources(tree, set()):
        try:
            module = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            opaque.append(path.relative_to(tree))
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.alias) and node.name == "*":
                opaque.append(path.relative_to(tree))
            elif isinstance(node, _DEFINITIONS):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[0])
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                names.add(node.rest)
    return names, opaque


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (prefix := _dotted(node.value)):
        return f"{prefix}.{node.attr}"
    return None


def _imports(module: ast.Module) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[str]]]:
    names: dict[str, set[tuple[str, str]]] = {}
    modules: dict[str, set[str]] = {}
    for statement in ast.walk(module):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".")[0]
                modules.setdefault(bound, set()).add(alias.name if alias.asname else bound)
        elif isinstance(statement, ast.ImportFrom) and statement.module and not statement.level:
            for alias in statement.names:
                bound = alias.asname or alias.name
                names.setdefault(bound, set()).add((statement.module, alias.name))
                modules.setdefault(bound, set()).add(f"{statement.module}.{alias.name}")
    return names, modules


def _test_function(module: ast.Module, scopes: list[str]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    body, node = module.body, None
    for scope in scopes:
        node = next((child for child in body if isinstance(child, _DEFINITIONS) and child.name == scope), None)
        if node is None:
            return None
        body = node.body
    return None if isinstance(node, ast.ClassDef) else node


def referenced_subjects(tree: Path, nodeid: str) -> set[tuple[str, str]]:
    path, *scopes = nodeid.split("[", 1)[0].split("::")
    try:
        module = ast.parse((tree / path).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    if (node := _test_function(module, scopes)) is None:
        return set()
    names, modules = _imports(module)
    subjects = set()
    for child in (descendant for statement in node.body for descendant in ast.walk(statement)):
        if isinstance(child, ast.Name):
            subjects.update(names.get(child.id, ()))
        elif isinstance(child, ast.Attribute) and (prefix := _dotted(child.value)):
            root, _, rest = prefix.partition(".")
            subjects.update((f"{bound}.{rest}" if rest else bound, child.attr) for bound in modules.get(root, ()))
    return subjects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head collects fewer tests than the base")
    parser.add_argument("--base", type=Path, required=True, help="checkout of the base revision")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--tests", default="tests/")
    args = parser.parse_args(argv)
    collected = collect_all({"base": args.base, "head": args.head}, args.tests)
    failed = [side for side, nodeids in collected.items() if not nodeids]
    for side in failed:
        print(f"::error::Collecting the {side} tests failed or found none, so the floor cannot be graded.")
    if failed:
        return 1
    base, head = collected["base"], collected["head"]
    lost = sorted(set(base) - set(head))
    admitted = {}
    if len(head) < len(base):
        surviving, opaque = bound_names(args.head)
        for path in opaque:
            print(f"{path} hides its names in the head (unparseable or a star import), so no lost test is admitted")
        definitions = set() if opaque else top_level_definitions(args.base, args.tests)
        deleted = {definition for definition in definitions if definition[1] not in surviving}
        for nodeid in lost:
            if subjects := sorted(".".join(subject) for subject in referenced_subjects(args.base, nodeid) & deleted):
                admitted[nodeid] = subjects
                print(f"{nodeid} is admitted: the head deletes {', '.join(subjects)}, which it referenced")
    floor = len(base) - len(admitted)
    print(f"head collects {len(head)} tests, base floor {floor}")
    if len(head) < floor:
        for nodeid in lost:
            if nodeid not in admitted:
                print(f"{nodeid} is in the base and not in the head")
        print(f"::error::The head collects {floor - len(head)} fewer tests than the base floor of {floor}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
