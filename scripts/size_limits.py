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
# -I keeps a ruff module in the graded tree from shadowing the real one.
_RUFF = (sys.executable, "-I", "-m", "ruff")


class GradeError(Exception):
    pass


def _ruff_version() -> str:
    return subprocess.run([*_RUFF, "--version"], capture_output=True, text=True).stdout.strip().removeprefix("ruff ")


def _python(path: Path) -> bool:
    if path.suffix in (".py", ".pyi", ".pyw"):
        return True
    with path.open("rb") as handle:
        first = handle.readline(256)
    return first.startswith(b"#!") and (b"python" in first or b"pypy" in first)


def files(tree: Path) -> list[Path]:
    result = subprocess.run(["git", "-C", str(tree), "ls-files", "-z"], capture_output=True)
    if result.returncode != 0:
        raise GradeError(f"git ls-files failed in {tree}: {result.stderr.decode().strip()}")
    tracked = (tree / name for name in result.stdout.decode().split("\0") if name)
    found = sorted(path for path in tracked if _python(path))
    if not found:
        raise GradeError(f"no tracked Python files under {tree}")
    return found


def functions(source: str) -> dict[int, tuple[str, int]]:
    found, seen = {}, {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name}#{seen[name]}"
                if not isinstance(child, ast.ClassDef):
                    found[child.lineno] = (name, child.end_lineno - child.lineno + 1)
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def _record(measured: dict, unit: str, rule: str, value: int) -> None:
    measured.setdefault(unit, {})[rule] = value


def _lengths(tree: Path, defs: dict[Path, dict[int, tuple[str, int]]], measured: dict) -> None:
    for path, units in defs.items():
        rel = path.relative_to(tree).as_posix()
        lines = len(path.read_text().splitlines())
        if lines > FILE_LINES:
            _record(measured, rel, "file-lines", lines)
        for name, length in units.values():
            if length > FUNCTION_LINES:
                _record(measured, f"{rel}::{name}", "function-lines", length)


def _ruff_hits(tree: Path, defs: dict[Path, dict[int, tuple[str, int]]], measured: dict) -> None:
    flags = ("--isolated", "--ignore-noqa", "--exit-zero")
    command = [*_RUFF, "check", *flags, "--output-format", "json", "--select", ",".join(RULES)]
    command += [f"--config={setting}" for setting in _SETTINGS] + [str(path) for path in defs]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise GradeError(f"ruff exited {result.returncode}: {result.stderr.strip()}")
    for hit in json.loads(result.stdout):
        path, row, message = Path(hit["filename"]), hit["location"]["row"], hit["message"]
        value = _MEASURED.search(message)
        if hit["code"] not in RULES or value is None or row not in defs.get(path, {}):
            raise GradeError(f"cannot grade {path}:{row}: {hit['code']} {message}")
        _record(measured, f"{path.relative_to(tree).as_posix()}::{defs[path][row][0]}", hit["code"], int(value[1]))


def measure(tree: Path) -> dict[str, dict[str, int]]:
    tree = tree.resolve()
    defs = {path: functions(path.read_text()) for path in files(tree)}
    measured = {}
    _lengths(tree, defs, measured)
    _ruff_hits(tree, defs, measured)
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


def _base(args) -> dict:
    base = load(args.base)
    if base is None:
        raise GradeError(f"the base has no {ALLOWLIST}, so the head cannot be graded")
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    if (version := _ruff_version()) != RUFF_VERSION:
        print(f"::error::The size gate needs ruff {RUFF_VERSION}, found {version or 'none'}, so it cannot grade.")
        return 1
    try:
        if args.write:
            write(args.head)
            return 0
        head = measure(args.head)
        recorded = load(args.head)
        base = _base(args)
    except (GradeError, SyntaxError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, head, recorded)
    print(f"{len(head)} units over a limit, {len(base)} on the base allowlist, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
