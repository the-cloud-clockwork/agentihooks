import argparse
import json
from pathlib import Path
from unittest.mock import patch

import vulture
from vulture.noqa import NOQA_REGEXP
from vulture.utils import ExitCode, VultureInputException, read_file

from scripts.size_limits import GradeError, files

VULTURE_VERSION = "2.14"
MIN_CONFIDENCE = 60
SCOPE = ("hooks/", "scripts/")
ALLOWLIST = Path("tests/DEAD_CODE_ALLOWLIST.json")


def _graded(tree: Path) -> list[Path]:
    graded = [path for path in files(tree) if path.relative_to(tree).as_posix().startswith(SCOPE)]
    if not graded:
        raise GradeError(f"no tracked Python files under {' or '.join(SCOPE)} in {tree}")
    return graded


def hits(tree: Path) -> dict[str, list[str]]:
    tree = tree.resolve()
    scanner = vulture.Vulture()
    # Vulture drops test_ functions and Test classes from any path holding /test, which hooks/ and scripts/ never are.
    with patch.object(vulture.core, "_is_test_file", frozenset().__contains__):
        for path in _graded(tree):
            try:
                code = read_file(path)
            except VultureInputException as exc:
                raise GradeError(f"vulture cannot read {path}: {exc}") from exc
            # A noqa comment in the graded tree hides nothing. Per line, since the pattern's trailing \s takes newlines.
            scanner.scan("\n".join(NOQA_REGEXP.sub("#", line) for line in code.split("\n")), filename=path)
        # Loads vulture's whitelists for the modules the graded files import.
        scanner.scavenge([])
    if scanner.exit_code == ExitCode.InvalidInput:
        raise GradeError(f"vulture cannot parse every graded file in {tree}; its error is above")
    found = {}
    # Vulture's own whitelists carry relative names; every graded file is absolute.
    graded = [item for item in scanner.get_unused_code(min_confidence=MIN_CONFIDENCE) if item.filename.is_absolute()]
    for item in graded:
        unit = item.filename.relative_to(tree).as_posix()
        found.setdefault(unit, []).append(f"{unit}:{item.first_lineno}: {item.message} ({item.confidence}% confidence)")
    return found


def measure(tree: Path) -> dict[str, int]:
    return {unit: len(found) for unit, found in hits(tree).items()}


def load(tree: Path) -> dict[str, int] | None:
    path = tree / ALLOWLIST
    if not path.is_file():
        return None
    recorded = json.loads(path.read_text())
    if not isinstance(recorded, dict) or not all(type(count) is int for count in recorded.values()):
        raise GradeError(f"{path} holds no object of per file counts")
    return recorded


def write(tree: Path) -> None:
    (tree / ALLOWLIST).write_text(json.dumps(measure(tree), indent=1, sort_keys=True) + "\n")


def grade(base: dict, measured: dict[str, int], head: dict[str, list[str]], recorded: dict) -> list[str]:
    # The base's measurement grades, not its record: lint skips the merge queue, so dev's record can go stale.
    errors = []
    for unit, found in sorted(head.items()):
        allowed = measured.get(unit, 0)
        if len(found) > allowed:
            errors.append(f"{unit} has {len(found)} unused names; the base holds {allowed}")
            errors.extend(found)
    for unit, written in sorted(recorded.items()):
        allowed = max(base.get(unit, 0), measured.get(unit, 0))
        if written > allowed:
            errors.append(f"{ALLOWLIST} records {written} for {unit}, above the base's {allowed}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    if vulture.__version__ != VULTURE_VERSION:
        print(f"::error::The dead code gate needs vulture {VULTURE_VERSION}, found {vulture.__version__}.")
        return 1
    try:
        if args.write:
            write(args.head)
            return 0
        head = hits(args.head)
        recorded = load(args.head) or {}
        base = load(Path(args.base)) or {}
        measured = measure(Path(args.base))
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, measured, head, recorded)
    print(f"{len(head)} files hold dead code, {len(measured)} on the base, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    if recorded != {unit: len(found) for unit, found in head.items()}:
        print(f"::notice::{ALLOWLIST} differs from the head's measurement; run python -m scripts.dead_code --write")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
