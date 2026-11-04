"""Hold the tick step pattern against a shrink only allowlist.

Per graded file it records: each top level def and class name in the tick module, each registered step
missing a budget row, an interval from the set, a lane or a needs, reads, writes, effects or feature
declaration, and each import of an adapted implementation from a module outside the adapters. A head may
hold only offences its base allowlist records for that file.
"""

import argparse
import ast
import json
from pathlib import Path

from scripts.size_limits import GradeError, files

SCOPE = ("scripts/tick/", "scripts/swarm/tick.py")
TICK = "scripts/swarm/tick.py"
BUDGETS = "scripts/tick/steps/budgets.py"
STEPS = "scripts/tick/"
ADAPTERS = "scripts/tick/adapters/"
ALLOWLIST = Path("tests/TICK_PATTERN_ALLOWLIST.json")
BANNED = (
    "scripts.inbox",
    "hooks.classifier",
    "scripts.swarm.metrics_outbox",
    "scripts.swarm.host_budget",
    "scripts.swarm.refresh_job",
    "scripts.swarm.snapshot_job",
)
INTERVALS = ("CRITICAL_S", "WIDE_S", "SLOW_S")
DECLARED = ("lane", "needs", "reads", "writes", "effects", "feature")
POSITIONS = {
    "CallStep": ("name", "group", "call", "needs", "reads", "writes", "lane"),
    "WriteStep": ("name", "group", "call", "writes", "needs", "reads"),
    "register": ("name",),
}


def _parse(name: str, source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise GradeError(f"cannot parse {name}: {exc}") from exc


def floors(source: str) -> dict[str, str]:
    """The interval name each step row of the budgets source declares, keyed by step name."""
    rows: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign):
            target, table = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, table = node.targets[0], node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == "MEASURED" and isinstance(table, ast.Dict)):
            continue
        for key, value in zip(table.keys, table.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            args = value.args if isinstance(value, ast.Call) else []
            interval = args[1] if len(args) > 1 else None
            rows[key.value] = interval.id if isinstance(interval, ast.Name) else ""
    return rows


def _callee(node: ast.Call) -> str:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return name if name in POSITIONS else ""


def steps(tree: ast.Module, rows: dict[str, str]) -> list[str]:
    """Offences of every step registered with a literal name in tree against the budget rows."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        if not callee or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        name = first.value
        given = set(POSITIONS[callee][: len(node.args)]) | {kw.arg for kw in node.keywords if kw.arg}
        if name not in rows:
            found.append(f"step {name}: no budget row")
        elif rows[name] not in INTERVALS:
            found.append(f"step {name}: interval outside {', '.join(INTERVALS)}")
        found += [f"step {name}: no {field}" for field in DECLARED if field not in given]
    return found


def banned(tree: ast.Module) -> list[str]:
    """Imports in tree that reach an adapted implementation, and every relative import."""
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level:
            found.append(f"import relative {'.' * node.level}{node.module or ''}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        found += [
            f"import {name}" for name in names if any(name == ban or name.startswith(f"{ban}.") for ban in BANNED)
        ]
    return found


def names(tree: ast.Module) -> list[str]:
    """Top level def and class names of the tick module."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return [f"def {node.name}" for node in tree.body if isinstance(node, kinds)]


def measure(tree: Path) -> dict[str, list[str]]:
    """Sorted offences per graded file of tree, only files holding any."""
    tree = tree.resolve()
    budgets = tree / BUDGETS
    rows = floors(budgets.read_text()) if budgets.is_file() else {}
    measured: dict[str, list[str]] = {}
    for path in files(tree):
        name = path.relative_to(tree).as_posix()
        if not name.startswith(SCOPE):
            continue
        parsed = _parse(name, path.read_text())
        found = steps(parsed, rows)
        if name == TICK:
            found += names(parsed)
        if name.startswith(STEPS) and not name.startswith(ADAPTERS):
            found += banned(parsed)
        if found:
            measured[name] = sorted(set(found))
    return measured


def load(tree: Path) -> dict[str, list[str]] | None:
    """The allowlist recorded in tree, or nothing when it has none."""
    path = tree / ALLOWLIST
    return json.loads(path.read_text()) if path.is_file() else None


def write(tree: Path) -> None:
    """Record the measurement of tree as its allowlist."""
    (tree / ALLOWLIST).write_text(json.dumps(measure(tree), indent=1, sort_keys=True) + "\n")


def grade(
    base: dict[str, list[str]] | None, head: dict[str, list[str]], recorded: dict[str, list[str]] | None
) -> list[str]:
    """Reject offences the base allowlist does not hold and a head record that differs from its measurement;
    a base without an allowlist allows what the head records."""
    allowed = (recorded or {}) if base is None else base
    errors = [
        f"{name} adds {offence}; the base allowlist does not hold it"
        for name, offences in sorted(head.items())
        for offence in offences
        if offence not in allowed.get(name, [])
    ]
    if recorded != head:
        errors.append(f"{ALLOWLIST} differs from the head's measurement; run python -m scripts.tick_pattern --write")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Grade the head against the base, or record the head's allowlist with --write."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head", default=".")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    tree = Path(args.head)
    try:
        if args.write:
            write(tree)
            return 0
        head, recorded, base = measure(tree), load(tree), load(Path(args.base))
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, head, recorded)
    held = sum(len(offences) for offences in head.values())
    allowed = sum(len(offences) for offences in (base or {}).values())
    print(f"{held} tick pattern offences in {len(head)} files, {allowed} on the base allowlist, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
