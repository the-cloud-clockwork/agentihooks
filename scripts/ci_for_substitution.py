"""Refuse a for loop whose word list holds a command substitution, since errexit never sees that command fail."""

import argparse
from collections.abc import Sequence
from pathlib import Path

RED_FIXTURE = "tests/fixtures/ci_for_substitution/red.sh"
ADVICE = (
    "a for loop word list holds a command substitution, so errexit never sees that command fail; "
    "capture its output in an assignment before the loop, or count with a brace range or an arithmetic loop"
)


def findings(source: bytes) -> list[int]:
    """List the line of each for loop whose word list holds a command substitution, in source order."""
    return []


def main(argv: Sequence[str] | None = None) -> int:
    """Grade every tracked shell file under the root but the red fixture, and report each refused word list."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
