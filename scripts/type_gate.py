"""Strict type gate: basedpyright error totals per file may only shrink against the base's baseline."""

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

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


def measure(head: Path) -> dict[str, int]:
    """Run basedpyright over the head and count its errors per file."""
    result = subprocess.run(
        [*_BASEDPYRIGHT, "--outputjson", "--project", str(head / "pyproject.toml"), "--pythonpath", sys.executable],
        cwd=head,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise GradeError(f"basedpyright exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    root = head.resolve()
    counts: dict[str, int] = {}
    for diagnostic in json.loads(result.stdout)["generalDiagnostics"]:
        if diagnostic["severity"] == "error":
            path = Path(diagnostic["file"]).resolve().relative_to(root).as_posix()
            counts[path] = counts.get(path, 0) + 1
    return dict(sorted(counts.items()))


def load(tree: Path) -> dict[str, int] | None:
    """Return the baseline recorded in a tree, or None when it has none."""
    path = tree / BASELINE
    return json.loads(path.read_text()) if path.is_file() else None


def write(head: Path) -> None:
    """Record the head's measurement as its baseline."""
    (head / BASELINE).write_text(json.dumps(measure(head), indent=2) + "\n")


def grade(base: dict[str, int], head: dict[str, int], recorded: dict[str, int] | None) -> list[str]:
    """Return every file whose errors grew against the base, and any drift between the head and its baseline."""
    errors = [
        f"{path} has {count} type errors, more than the {base.get(path, 0)} the base allows"
        for path, count in head.items()
        if count > base.get(path, 0)
    ]
    recorded = recorded or {}
    errors += [
        f"{BASELINE} records {recorded.get(path, 0)} type errors in {path}, the head has {head.get(path, 0)}; "
        "run python -m scripts.type_gate --write"
        for path in sorted(set(recorded) | set(head))
        if recorded.get(path, 0) != head.get(path, 0)
    ]
    return errors


def main(argv: list[str] | None = None) -> int:
    """Grade the head against the base, or record the head's baseline with --write."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--measured", type=Path)
    parser.add_argument("--write", action="store_true")
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
        head = measure(args.head)
        recorded = load(args.head)
        base = load(args.base)
    except (GradeError, ValueError, KeyError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    if args.measured:
        args.measured.parent.mkdir(parents=True, exist_ok=True)
        args.measured.write_text(json.dumps(head, indent=2) + "\n")
    if base is None:
        print(f"::notice::The base carries no {BASELINE}; this head introduces it.")
        base = recorded or {}
    errors = grade(base, head, recorded)
    print(f"{sum(head.values())} type errors in {len(head)} files, {sum(base.values())} allowed, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
