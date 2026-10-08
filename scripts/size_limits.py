import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

RUFF_VERSION = "0.15.12"
RULES = {"C901": 12, "PLR0912": 12, "PLR0913": 7, "PLR0915": 300}
FUNCTION_LINES = 300
FILE_LINES = 3000
ALLOWLIST = Path("tests/SIZE_ALLOWLIST.json")
_SETTINGS = (
    f"lint.mccabe.max-complexity={RULES['C901']}",
    f"lint.pylint.max-branches={RULES['PLR0912']}",
    f"lint.pylint.max-args={RULES['PLR0913']}",
    f"lint.pylint.max-statements={RULES['PLR0915']}",
)
_MEASURED = re.compile(r"\((\d+) > \d+\)")


class GradeError(Exception):
    pass


def _ruff(tree: Path, *args: str) -> str:
    # --isolated: the grader's limits and file walk apply, never the graded tree's config.
    command = [sys.executable, "-m", "ruff", "check", "--isolated", *args]
    result = subprocess.run(command, cwd=tree, capture_output=True, text=True)
    if result.returncode != 0:
        raise GradeError(f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _ruff_version() -> str:
    result = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True)
    return result.stdout.split()[-1] if result.returncode == 0 else ""


def _functions(source: str) -> dict[int, tuple[str, int]]:
    found = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    found[child.lineno] = (name, child.end_lineno - child.lineno + 1)
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def _record(measured: dict, unit: str, rule: str, value: int) -> None:
    rules = measured.setdefault(unit, {})
    rules[rule] = max(value, rules.get(rule, 0))


def measure(tree: Path) -> dict[str, dict[str, int]]:
    tree = tree.resolve()
    shown = (Path(line).resolve() for line in _ruff(tree, "--show-files", ".").splitlines() if line.strip())
    files = sorted(path for path in shown if path.suffix in (".py", ".pyi"))
    if not files:
        raise GradeError(f"ruff found no Python files under {tree}")
    functions = {path: _functions(path.read_text()) for path in files}
    measured = {}
    for path, defs in functions.items():
        rel = path.relative_to(tree).as_posix()
        lines = len(path.read_text().splitlines())
        if lines > FILE_LINES:
            _record(measured, rel, "file-lines", lines)
        for name, length in defs.values():
            if length > FUNCTION_LINES:
                _record(measured, f"{rel}::{name}", "function-lines", length)
    report = _ruff(
        tree,
        "--ignore-noqa",
        "--exit-zero",
        "--output-format",
        "json",
        "--select",
        ",".join(RULES),
        *(f"--config={setting}" for setting in _SETTINGS),
        ".",
    )
    for hit in json.loads(report):
        path, row, message = Path(hit["filename"]).resolve(), hit["location"]["row"], hit["message"]
        value = _MEASURED.search(message)
        if hit["code"] not in RULES or value is None or row not in functions.get(path, {}):
            raise GradeError(f"cannot grade {path}:{row}: {hit['code']} {message}")
        _record(measured, f"{path.relative_to(tree).as_posix()}::{functions[path][row][0]}", hit["code"], int(value[1]))
    return measured


def load(tree: Path) -> dict[str, dict[str, int]] | None:
    path = tree / ALLOWLIST
    return json.loads(path.read_text()) if path.is_file() else None


def write(tree: Path) -> None:
    (tree / ALLOWLIST).write_text(json.dumps(measure(tree), indent=1, sort_keys=True) + "\n")


def grade(base: dict, head: dict, recorded: dict | None) -> list[str]:
    errors = []
    for unit, rules in sorted(head.items()):
        for rule, value in sorted(rules.items()):
            allowed = base.get(unit, {}).get(rule)
            if allowed is None:
                errors.append(f"{unit} breaks {rule} at {value}; it is not on the base allowlist")
            elif value > allowed:
                errors.append(f"{unit} grew on {rule} from {allowed} to {value}")
    if recorded != head:
        errors.append(f"{ALLOWLIST} differs from the head's measurement; run python -m scripts.size_limits --write")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hold the size and complexity limits on a shrink only allowlist")
    parser.add_argument("--base", type=Path, help="checkout of the base revision, whose allowlist grades the head")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--write", action="store_true", help="record the head's offenders as its allowlist")
    args = parser.parse_args(argv)
    if (version := _ruff_version()) != RUFF_VERSION:
        print(f"::error::The size gate needs ruff {RUFF_VERSION}, found {version or 'none'}, so it cannot grade.")
        return 1
    try:
        if args.write:
            write(args.head)
            return 0
        head = measure(args.head)
    except (GradeError, SyntaxError) as exc:
        print(f"::error::{exc}")
        return 1
    recorded = load(args.head)
    base = load(args.base) if args.base else None
    if base is None:
        base = recorded or {}
    errors = grade(base, head, recorded)
    print(f"{len(head)} units over a limit, {len(base)} on the base allowlist, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
