"""Refuse a changelog fragment a pull request adds unless it is named from the branch and opens with a section heading."""

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

FOLDER = "changelog.d"
SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")


def expected_name(branch: str) -> str:
    """Return the one fragment path a pull request from this branch may add."""
    return f"{FOLDER}/{branch.replace('/', '-')}.md"


def section(line: str) -> str | None:
    """Return the section a heading line names as the release notes read it, or None for an entry line."""
    rest = line.lstrip("#")
    if len(line) - len(rest) not in (2, 3) or not rest[:1].isspace():
        return None
    return rest.strip() or (rest[-1] if len(rest) > 1 else None)


def heading_problems(text: str) -> list[str]:
    """List why a fragment's entries would ship outside a Keep a Changelog section, in line order."""
    lines = [line for line in text.splitlines() if line.strip()]
    problems: list[str] = []
    if not lines or section(lines[0]) is None:
        problems.append("its first line is no section heading, so the release notes file its entries under Changed")
    for line in lines:
        name = section(line)
        if name is not None and name not in SECTIONS:
            problems.append(f"the heading {name} names none of the sections {', '.join(SECTIONS)}")
    return problems


def added(base: Path, head: Path) -> list[str]:
    """List the files the head adds under the changelog folder, relative to the head and in path order."""
    for tree in (base, head):
        if not (tree / FOLDER).is_dir():
            raise ValueError(f"{tree} carries no {FOLDER} folder, so the check graded no fragment")
    files = sorted(path.relative_to(head).as_posix() for path in (head / FOLDER).rglob("*") if path.is_file())
    return [name for name in files if not (base / name).exists()]


def grade(base: Path, head: Path, branch: str, required: bool) -> tuple[list[str], list[str]]:
    """Return the added fragments and one problem per misnamed fragment, heading fault and missing required fragment."""
    if not branch:
        raise ValueError("no branch name was given, so the check cannot name the fragment a pull request adds")
    expected = expected_name(branch)
    fragments = added(base, head)
    problems: list[str] = []
    for name in fragments:
        if name != expected:
            problems.append(f"{name}: a pull request from the branch {branch} adds its fragment as {expected}")
        problems += [f"{name}: {problem}" for problem in heading_problems((head / name).read_text())]
    if required and expected not in fragments:
        problems.append(f"{expected}: a pull request from the branch {branch} adds no fragment at this path")
    return fragments, problems


def main(argv: Sequence[str] | None = None) -> int:
    """Grade the fragments the head adds against the base and report each refusal."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv)
    try:
        fragments, problems = grade(
            args.base, args.head, args.branch, os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
        )
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"Changelog fragments: {len(fragments)} added, {len(problems)} problems")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
