"""Strict type gate: basedpyright error totals per file may only shrink against the base's baseline."""

import argparse
import ast
import json
import os
import subprocess
import sys
import tomllib
from itertools import accumulate
from pathlib import Path, PurePosixPath

BASEDPYRIGHT_VERSION = "1.40.2"
BASELINE = Path("tests/TYPE_BASELINE.json")
CONFIG = {
    "include": ["hooks", "scripts", "tests"],
    "pythonVersion": "3.12",
    "pythonPlatform": "Linux",
    "typeCheckingMode": "standard",
    "strict": ["hooks", "scripts"],
}
_BASEDPYRIGHT = (sys.executable, "-I", "-m", "basedpyright")
_NODE_OPTIONS = "--max-old-space-size=6656"


class GradeError(Exception):
    pass


def _basedpyright_version() -> str:
    """Return the installed basedpyright version, or an empty string when it is missing."""
    result = subprocess.run([*_BASEDPYRIGHT, "--version"], capture_output=True, text=True)
    return result.stdout.partition("\n")[0].strip().removeprefix("basedpyright ")


def config_errors(head: Path) -> list[str]:
    """Return an error when the head's basedpyright settings differ from the gate's."""
    settings = tomllib.loads((head / "pyproject.toml").read_text()).get("tool", {}).get("basedpyright")
    if settings == CONFIG:
        return []
    return [f"pyproject.toml [tool.basedpyright] is {settings!r}; the type gate grades only with {CONFIG!r}"]


def sources(head: Path) -> list[str]:
    """Return every checked source under the included folders, sorted so each run checks them in one order.

    basedpyright infers some types differently depending on which file it checks first, and the order it lists a
    folder in follows the filesystem, so an unsorted run can count other errors on another runner.
    """
    return sorted(
        path.relative_to(head).as_posix()
        for folder in CONFIG["include"]
        for path in (head / folder).rglob("*.py*")
        if path.suffix in (".py", ".pyi")
        and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(head).parts)
    )


def measure(head: Path, files: list[str] | None = None) -> dict[str, int]:
    """Run basedpyright over the given sources, every source when None, in sorted order and count its errors per file.

    An empty list measures nothing. The checker empties its type cache once the Node heap passes ninety percent of
    its limit, and when that happens varies between runs, so the heap limit is raised well above what the check uses.
    """
    files = sources(head) if files is None else sorted(files)
    if not files:
        return {}
    result = subprocess.run(
        [
            *_BASEDPYRIGHT,
            "--outputjson",
            "--project",
            str(head / "pyproject.toml"),
            "--pythonpath",
            sys.executable,
            *files,
        ],
        cwd=head,
        env={**os.environ, "NODE_OPTIONS": _NODE_OPTIONS},
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise GradeError(f"basedpyright exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise GradeError(f"basedpyright printed no report: {result.stderr.strip()}") from exc
    if not report["summary"]["filesAnalyzed"]:
        raise GradeError("basedpyright analysed no files")
    root = head.resolve()
    counts: dict[str, int] = {}
    for diagnostic in report["generalDiagnostics"]:
        if diagnostic["severity"] == "error":
            path = Path(diagnostic["file"]).resolve().relative_to(root).as_posix()
            counts[path] = counts.get(path, 0) + 1
    return dict(sorted(counts.items()))


def load(tree: Path) -> dict[str, int] | None:
    """Return the baseline recorded in a tree, or None when it has none."""
    path = tree / BASELINE
    return json.loads(path.read_text()) if path.is_file() else None


def changed(base: Path, head: Path, paths: set[str]) -> set[str]:
    """Return the paths whose contents differ between the base and head trees."""
    return {path for path in paths if _contents(base / path) != _contents(head / path)}


def _contents(path: Path) -> bytes | None:
    """Return a file's bytes, or None when it does not exist."""
    return path.read_bytes() if path.is_file() else None


def module_name(path: str) -> str:
    """Return the dotted module a source path defines, its package for an __init__ file."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported(path: str, source: bytes) -> set[str]:
    """Return every module a source imports, each enclosing package included, with relative imports resolved."""
    package = module_name(path).split(".")
    if PurePosixPath(path).stem != "__init__":
        package.pop()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[: len(package) - node.level + 1] if node.level else []
            target = ".".join([*prefix, *([node.module] if node.module else [])])
            names.add(target)
            names.update(f"{target}.{alias.name}" for alias in node.names)
    return {prefix for name in names for prefix in accumulate(name.split("."), lambda left, right: f"{left}.{right}")}


def importers(head: Path, files: list[str], modules: set[str]) -> set[str]:
    """Return every source that imports one of the modules, directly or through another source that does."""
    imports: dict[str, set[str]] = {}
    for path in files:
        try:
            imports[path] = imported(path, (head / path).read_bytes())
        except SyntaxError:
            imports[path] = set()
    found: set[str] = set()
    frontier = modules
    for _ in files:
        reached = {path for path, names in imports.items() if path not in found and names & frontier}
        found |= reached
        frontier = {module_name(path) for path in reached}
    return found


def graded(base: Path, head: Path) -> set[str]:
    """Return the sources the gate grades for growth: changed ones and every source that imports them."""
    files = sources(head)
    touched = changed(base, head, set(files) | set(sources(base)))
    return touched | importers(head, files, {module_name(path) for path in touched})


def edited(base_baseline: dict[str, int], recorded: dict[str, int]) -> set[str]:
    """Return the baseline entries whose count the head's baseline changes against the base's."""
    return {path for path in set(base_baseline) | set(recorded) if base_baseline.get(path, 0) != recorded.get(path, 0)}


def write(head: Path) -> None:
    """Record the head's measurement as its baseline."""
    (head / BASELINE).write_text(json.dumps(measure(head), indent=2) + "\n")


def _drifted(recorded: int, head: int, base: int) -> bool:
    """Return whether an entry records fewer errors than the head has, or more than both the head and the base."""
    return recorded < head or recorded > max(head, base)


def grade(
    base: dict[str, int], head: dict[str, int], recorded: dict[str, int] | None, touched: set[str] | None = None
) -> list[str]:
    """Return every changed file whose errors grew against the base, and drift between the head and its baseline.

    Growth and drift are graded on ``touched``, the files the head changes and their importers, drift also on any
    baseline entry it edits; with ``touched`` None, on every file, and drift then means any difference. With
    ``touched`` given, an entry above the head's count but not above the base's is a ceiling the head keeps under, so
    a head that fixes errors needs no baseline edit and pull requests fixing different files never edit the file.
    An untouched file keeps the count the base recorded until a baseline refresh from the gate's own measurement
    rewrites it, so one merge order cannot turn every later head red.
    """
    errors = [
        f"{path} has {count} type errors, more than the {base.get(path, 0)} the base allows"
        for path, count in head.items()
        if count > base.get(path, 0) and (touched is None or path in touched)
    ]
    recorded = recorded or {}
    errors += [
        f"{BASELINE} records {recorded.get(path, 0)} type errors in {path}, the head has {head.get(path, 0)}; "
        "run python -m scripts.type_gate --write"
        for path in sorted(set(recorded) | set(head))
        if (
            recorded.get(path, 0) != head.get(path, 0)
            if touched is None
            else (
                _drifted(recorded.get(path, 0), head.get(path, 0), base.get(path, 0))
                and (path in touched or recorded.get(path, 0) != base.get(path, 0))
            )
        )
    ]
    return errors


def build_parser() -> argparse.ArgumentParser:
    """Return the command line parser for the gate."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--measured", type=Path)
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Grade the head against the base, or record the head's baseline with --write."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    if (version := _basedpyright_version()) != BASEDPYRIGHT_VERSION:
        print(f"::error::The type gate needs basedpyright {BASEDPYRIGHT_VERSION}, found {version or 'none'}.")
        return 1
    try:
        if errors := config_errors(args.head):
            print(f"::error::{errors[0]}")
            return 1
        if args.write:
            write(args.head)
            return 0
        recorded = load(args.head)
        base = load(args.base)
        touched = checked = None
        if base is not None:
            touched = graded(args.base, args.head)
            checked = touched | edited(base, recorded or {})
        head = measure(args.head, None if checked is None else [path for path in sources(args.head) if path in checked])
    except (GradeError, ValueError, KeyError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    if args.measured:
        kept = {path: count for path, count in (recorded or {}).items() if checked is not None and path not in checked}
        args.measured.parent.mkdir(parents=True, exist_ok=True)
        args.measured.write_text(json.dumps(dict(sorted({**kept, **head}.items())), indent=2) + "\n")
    if base is None:
        print(f"::notice::The base carries no {BASELINE}; this head introduces it.")
        base = recorded or {}
    errors = grade(base, head, recorded, touched)
    print(f"{sum(head.values())} type errors in {len(head)} files, {sum(base.values())} allowed, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
