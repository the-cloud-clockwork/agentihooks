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
DISABLING = {"skip", "skipif", "xfail", "importorskip", "exit"}
CHECKING_CALLS = {"raises", "warns", "deprecated_call", "fail"}
CONFIGS = (
    "conftest.py",
    "*.toml",
    "*.ini",
    "*.cfg",
    "tests/**/conftest.py",
    "tests/**/*.toml",
    "tests/**/*.ini",
    "tests/**/*.cfg",
    ".github/**/*.yml",
    ".github/**/*.yaml",
)
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class GradeError(Exception):
    pass


def _named(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _nodes(statements: list[ast.AST]) -> list[ast.AST]:
    return [node for statement in statements for node in ast.walk(statement)]


def _disables(nodes: list[ast.AST], skippers: set[str]) -> bool:
    return any(
        _named(node) in DISABLING | skippers or (isinstance(node, ast.Constant) and node.value in DISABLING)
        for node in nodes
    )


def _assertions(nodes: list[ast.AST], helpers: frozenset[str]) -> int:
    return sum(
        isinstance(node, ast.Assert)
        or (
            isinstance(node, ast.Call)
            and (
                _named(node.func) in CHECKING_CALLS | helpers
                or (isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert"))
            )
        )
        for node in nodes
    )


def _returns_early(statements: list[ast.stmt]) -> bool:
    pending: list[ast.AST] = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return):
            return True
        if not isinstance(node, (*DEFINITIONS, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return False


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


def _cases(context: list[ast.AST], constants: dict[str, ast.AST]) -> int:
    cases = 1
    for node in context:
        if isinstance(node, ast.Call) and _named(node.func) == "parametrize":
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            values = node.args[1] if len(node.args) > 1 else keywords.get("argvalues")
            cases *= _length(values, constants) or 1
    return cases


def _functions(body: list[ast.stmt], prefix: str, inherited: list[ast.AST]):
    # Module and class statements carry pytestmark and stored marks, so they disable every test below them.
    context = [*inherited, *(node for node in body if not isinstance(node, DEFINITIONS))]
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{node.name}", node, [*context, *node.decorator_list]
        elif isinstance(node, ast.ClassDef):
            yield from _functions(node.body, f"{prefix}{node.name}::", [*context, *node.decorator_list])


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
        functions = list(_functions(module.body, f"{name}::", []))
        bodies = {key: _nodes(function.body) for key, function, _ in functions}
        helpers = frozenset(function.name for key, function, _ in functions if _assertions(bodies[key], frozenset()))
        skippers = {
            alias.asname or alias.name
            for node in ast.walk(module)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name in DISABLING
        }
        for _ in functions:
            skippers |= {function.name for key, function, _ in functions if _disables(bodies[key], skippers)}
        for key, function, context in functions:
            test = function.name.startswith("test")
            assertions = _assertions(bodies[key], helpers)
            if not (test or assertions):
                continue
            measured[key] = {
                "assertions": assertions,
                "calls": sum(isinstance(node, ast.Call) for node in bodies[key]),
                "cases": _cases(context, constants),
                "disabled": test
                and (_disables([*bodies[key], *_nodes(context)], skippers) or _returns_early(function.body)),
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
        raise GradeError(f"{MANIFEST} must map each locked test to its assertions, calls and cases")
    return data


def write(tree: Path) -> None:
    entries = {
        key: {field: entry[field] for field in FIELDS}
        for key, entry in sorted(measure(tree).items())
        if not entry["disabled"]
    }
    (tree / MANIFEST).write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n")


def unlocking_configs(tree: Path) -> list[str]:
    paths = sorted({path for pattern in CONFIGS for path in tree.glob(pattern) if path.is_file()})
    return [
        f"{path.relative_to(tree)} names the locked module {stem}; a locked test cannot be ignored or deselected."
        for path in paths
        for stem in (Path(name).stem for name in LOCKED)
        if stem in path.read_text()
    ]


def grade(base: dict, recorded: dict, head: dict) -> list[str]:
    errors = [
        f"The locked test {key} is skipped, xfailed or returns early."
        for key, found in sorted(head.items())
        if found["disabled"]
    ]
    for key, floor in sorted(base.items()):
        found = head.get(key)
        if found is None:
            errors.append(f"The locked test {key} is deleted or renamed.")
        for field in FIELDS:
            if found is not None and found[field] < floor[field]:
                errors.append(f"The locked test {key} has {found[field]} {field}, the base locks {floor[field]}.")
            if key not in recorded or recorded[key][field] < floor[field]:
                errors.append(f"The head {MANIFEST} drops or lowers the locked {field} of {key}.")
    for key, entry in sorted(recorded.items()):
        found = head.get(key)
        if found is None or any(entry[field] > found[field] for field in FIELDS):
            errors.append(
                f"The head {MANIFEST} records more than {key} measures; run python -m scripts.locked_tests --write."
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
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
        configs = unlocking_configs(args.head)
        recorded = load(args.head)
        if recorded is None:
            raise GradeError(f"the head has no {MANIFEST}")
        base = load(Path(args.base))
        if base is None:
            # Only the pull request that introduces the manifest meets a base without one.
            print(f"::notice::The base has no {MANIFEST}; the head manifest locks itself.")
            base = recorded
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, recorded, head) + configs
    print(f"{len(base)} locked tests on the base, {len(head)} measured on the head, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
