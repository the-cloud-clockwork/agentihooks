"""Shared CLI, load and write plumbing for a shrink only allowlist gate."""

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from scripts.size_limits import GradeError


def load_allowlist(tree: Path, allowlist: Path) -> dict[str, int] | None:
    """The allowlist recorded at allowlist under tree, or nothing when it has none."""
    path = tree / allowlist
    return json.loads(path.read_text()) if path.is_file() else None


def write_allowlist(tree: Path, allowlist: Path, measured: dict[str, int]) -> None:
    """Record measured as tree's allowlist at allowlist."""
    (tree / allowlist).write_text(json.dumps(measured, indent=1) + "\n")


def main(
    argv: list[str] | None,
    allowlist: Path,
    measure: Callable[[Path], dict[str, int]],
    grade: Callable[[dict[str, int] | None, dict[str, int], dict[str, int] | None], list[str]],
    noun: str,
) -> int:
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
            write_allowlist(tree, allowlist, measure(tree))
            return 0
        head, recorded, base = (
            measure(tree),
            load_allowlist(tree, allowlist),
            load_allowlist(Path(args.base), allowlist),
        )
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, head, recorded)
    print(f"{len(head)} files {noun}, {len(base or {})} on the base allowlist, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0
