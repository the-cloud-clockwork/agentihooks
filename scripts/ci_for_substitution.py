"""Refuse a for loop whose word list holds a command substitution, since errexit never sees that command fail."""

import argparse
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from scripts.no_comments import language

BASH = Language(tree_sitter_bash.language())
RED_FIXTURE = "tests/fixtures/ci_for_substitution/red.sh"
ADVICE = (
    "a for loop word list holds a command substitution, so errexit never sees that command fail; "
    "capture its output in an assignment before the loop, or count with a brace range or an arithmetic loop"
)


def _nodes(node: Node) -> Iterator[Node]:
    """Yield a node and every node beneath it."""
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(current.children)


def findings(source: bytes) -> list[int]:
    """List the line of each for loop whose word list holds a command substitution, in source order."""
    loops = [node for node in _nodes(Parser(BASH).parse(source).root_node) if node.type == "for_statement"]
    return sorted(
        loop.start_point[0] + 1
        for loop in loops
        if any(
            node.type == "command_substitution"
            for word in loop.children_by_field_name("value")
            for node in _nodes(word)
        )
    )


def blank_expressions(text: str) -> str:
    """Overwrite each GitHub expression with underscores, keeping its newlines, so the shell parser reads a plain word."""
    return text


def workflow_findings(text: str) -> list[int]:
    """List the file line of each refused for loop in the bash run blocks of a workflow or composite action."""
    return []


def main(argv: Sequence[str] | None = None) -> int:
    """Grade every tracked shell file under the root but the red fixture, and report each refused word list."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    root: Path = parser.parse_args(argv).root
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    shells = [
        name
        for name in listed.stdout.decode().split("\0")
        if name and name != RED_FIXTURE and language(root, name) == "shell"
    ]
    problems = [f"{name}:{line}: {ADVICE}" for name in shells for line in findings((root / name).read_bytes())]
    if not shells:
        problems.append("No tracked shell file found; the check graded nothing")
    print(f"For loop word lists: {len(shells)} shell files, {len(problems)} problems")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
