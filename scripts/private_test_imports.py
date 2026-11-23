"""Gate the private names test modules import from other test modules."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

RULE = Path(".semgrep/test-private-imports.yml")
ALLOWLIST = Path(".semgrep/test-private-imports-allowlist.json")
TESTS = Path("tests")


def configs(grader: Path | None) -> list[Path]:
    """Return the rule configs a scan reads."""
    raise NotImplementedError


def scan(rules: Sequence[Path], targets: Sequence[Path], baseline: str | None = None) -> dict[str, Any]:
    """Return Semgrep's JSON report of the rules over the targets."""
    raise NotImplementedError


def offenders(report: dict[str, Any], root: Path) -> set[str]:
    """Return a file and name key for each private name the report's findings span."""
    raise NotImplementedError


def grade(new: set[str], current: set[str], allowed: set[str]) -> list[str]:
    """Return the errors of a scan against the list."""
    raise NotImplementedError


def check(baseline: str, grader: Path | None, root: Path) -> int:
    """Print each error and return 1 when there is one."""
    raise NotImplementedError


def write(root: Path) -> int:
    """Rewrite the list from a scan of every test module."""
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check or write command."""
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
