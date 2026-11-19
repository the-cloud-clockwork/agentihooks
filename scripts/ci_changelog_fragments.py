"""Refuse a changelog fragment a pull request adds unless it is named from the branch and opens with a section heading."""

import re
from collections.abc import Sequence

SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
HEADING = re.compile(r"#{2,3}\s+(.+?)\s*")


def expected_name(branch: str) -> str:
    """Return the one fragment path a pull request from this branch may add."""
    raise NotImplementedError(branch)


def heading_problems(text: str) -> list[str]:
    """List why a fragment's entries would ship outside a Keep a Changelog section, in line order."""
    raise NotImplementedError(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Grade the fragments the head adds against the base and report each refusal."""
    raise NotImplementedError(argv)


if __name__ == "__main__":
    raise SystemExit(main())
