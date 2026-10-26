import argparse
import ast
import json
from pathlib import Path

LOCKED = (
    "tests/test_secrets.py",
    "tests/context/test_branch_guard.py",
    "tests/context/test_prod_lockdown.py",
    "tests/test_kubectl_mutation_guard.py",
    "tests/test_credential_guard.py",
)
MANIFEST = Path("tests/LOCKED_MANIFEST.json")
FIELDS = ("assertions", "calls", "cases")
DISABLING_MARKS = {"skip", "skipif", "xfail"}
DISABLING_CALLS = {"skip", "xfail", "importorskip"}
CHECKING_CALLS = {"raises", "warns", "deprecated_call", "fail"}


class GradeError(Exception):
    pass


def _named(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _disables(expression: ast.AST, constants: dict[str, ast.AST]) -> bool:
    nodes = list(ast.walk(expression))
    nodes += [inner for node in nodes if _named(node) in constants for inner in ast.walk(constants[_named(node)])]
    return any(_named(node) in DISABLING_MARKS for node in nodes)


def _assertions(function: ast.AST, helpers: frozenset[str] = frozenset()) -> int:
    count = 0
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):
            count += 1
        elif isinstance(node, ast.Call) and (name := _named(node.func)):
            count += name in CHECKING_CALLS or name in helpers or name.lstrip("_").startswith("assert")
    return count


def _skips_itself(function: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and _named(node.func) in DISABLING_CALLS
        and (isinstance(node.func, ast.Name) or _named(node.func.value) == "pytest")
        for node in ast.walk(function)
    )


def _length(node: ast.AST | None, constants: dict[str, ast.AST], seen: frozenset[str] = frozenset()) -> int | None:
    if isinstance(node, ast.Name):
        if node.id in seen:
            return None
        seen, node = seen | {node.id}, constants.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _length(node.left, constants, seen), _length(node.right, constants, seen)
        return None if left is None or right is None else left + right
    return None


def _cases(decorators: list[ast.expr], constants: dict[str, ast.AST]) -> int:
    cases = 1
    for decorator in decorators:
        if isinstance(decorator, ast.Call) and _named(decorator.func) == "parametrize":
            keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
            values = decorator.args[1] if len(decorator.args) > 1 else keywords.get("argvalues")
            cases *= _length(values, constants) or 1
    return cases


def _marks(body: list[ast.stmt]) -> list[ast.expr]:
    return [
        statement.value
        for statement in body
        if isinstance(statement, ast.Assign) and any(_named(target) == "pytestmark" for target in statement.targets)
    ]


def _functions(body: list[ast.stmt], prefix: str, inherited: list[ast.expr]):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{node.name}", node, [*inherited, *node.decorator_list]
        elif isinstance(node, ast.ClassDef):
            yield from _functions(
                node.body, f"{prefix}{node.name}::", [*inherited, *node.decorator_list, *_marks(node.body)]
            )


def measure(tree: Path) -> dict[str, dict]:
    measured = {}
    for name in LOCKED:
        path = tree / name
        if not path.is_file():
            continue
        try:
            module = ast.parse(path.read_text(), filename=name)
        except SyntaxError as exc:
            raise GradeError(f"{name} does not parse: {exc}") from exc
        constants = {
            target.id: statement.value
            for statement in module.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        functions = list(_functions(module.body, f"{name}::", _marks(module.body)))
        helpers = frozenset(
            function.name
            for _, function, _ in functions
            if not function.name.startswith("test") and _assertions(function)
        )
        for key, function, decorators in functions:
            test = function.name.startswith("test")
            assertions = _assertions(function, helpers) if test else _assertions(function)
            if not (test or assertions):
                continue
            measured[key] = {
                "assertions": assertions,
                "calls": sum(isinstance(node, ast.Call) for node in ast.walk(function)),
                "cases": _cases(decorators, constants) if test else 1,
                "disabled": test
                and (any(_disables(mark, constants) for mark in decorators) or _skips_itself(function)),
            }
    return measured


def load(tree: Path) -> dict[str, dict] | None:
    path = tree / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise GradeError(f"{MANIFEST} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(entry, dict) and all(isinstance(entry.get(field), int) for field in FIELDS)
        for entry in data.values()
    ):
        raise GradeError(f"{MANIFEST} must map each locked test to its {', '.join(FIELDS)} counts")
    return data


def write(tree: Path) -> None:
    entries = {
        key: {field: entry[field] for field in FIELDS}
        for key, entry in sorted(measure(tree).items())
        if not entry["disabled"]
    }
    (tree / MANIFEST).write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n")


def grade(base: dict, recorded: dict, head: dict) -> list[str]:
    errors = []
    for key, floor in sorted(base.items()):
        found = head.get(key)
        if found is None:
            errors.append(f"The locked test {key} is deleted or renamed.")
            continue
        if found["disabled"]:
            errors.append(f"The locked test {key} is skipped or xfailed.")
        for field in FIELDS:
            if found[field] < floor[field]:
                errors.append(f"The locked test {key} has {found[field]} {field}, the base locks {floor[field]}.")
            if recorded.get(key, {}).get(field, -1) < floor[field]:
                errors.append(f"The head {MANIFEST} drops or lowers the locked {field} of {key}.")
    for key, entry in sorted(recorded.items()):
        found = head.get(key)
        if found is None or any(entry.get(field, 0) > found[field] for field in FIELDS):
            errors.append(
                f"The head {MANIFEST} records more than {key} measures; run python -m scripts.locked_tests --write."
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    try:
        if args.write:
            write(args.head)
            return 0
        head = measure(args.head)
        recorded = load(args.head)
        if recorded is None:
            raise GradeError(f"the head has no {MANIFEST}")
        base = load(args.base)
        if base is None:
            # Only the pull request that introduces the manifest meets a base without one.
            print(f"::notice::The base has no {MANIFEST}; the head manifest locks itself.")
            base = recorded
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, recorded, head)
    print(f"{len(base)} locked tests on the base, {len(head)} measured on the head, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
