"""Refuse a for loop whose word list holds a command substitution, since errexit never sees that command fail."""

import argparse
import re
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

import tree_sitter_bash
import yaml
from tree_sitter import Language, Node, Parser

from scripts.no_comments import language

BASH = Language(tree_sitter_bash.language())
RED_FIXTURE = "tests/fixtures/ci_for_substitution/red.sh"
WORKFLOWS = PurePosixPath(".github/workflows")
ACTIONS = ("action.yml", "action.yaml")
SHELL = re.compile(r"(?:\S*/)?(?:bash|sh)(?:\s|$)|\$\{\{")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
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
    """Overwrite each GitHub expression with underscores, keeping its newlines, so the shell parser reads one word."""
    return EXPRESSION.sub(lambda match: re.sub(r"[^\n]", "_", match.group()), text)


def _scalar(node: yaml.Node | None) -> str:
    """Return the text of a scalar node, or an empty string for any other node."""
    return node.value if isinstance(node, yaml.ScalarNode) else ""


def _field(node: yaml.Node | None, *keys: str) -> yaml.Node | None:
    """Follow mapping keys down from a node, or return None where one is missing."""
    for key in keys:
        if not isinstance(node, yaml.MappingNode):
            return None
        node = next((value for name, value in node.value if _scalar(name) == key), None)
    return node


def _run_blocks(node: yaml.Node | None, shell: str) -> Iterator[yaml.ScalarNode]:
    """Yield every run text beneath a node whose shell is bash, sh or a GitHub expression known only at run time.

    A step's own shell wins over the nearest default; a blank one runs the default, as an absent one does.
    """
    if isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _run_blocks(item, shell)
    elif isinstance(node, yaml.MappingNode):
        shell = _scalar(_field(node, "defaults", "run", "shell")).strip() or shell
        run = _field(node, "run")
        if isinstance(run, yaml.ScalarNode) and SHELL.match(_scalar(_field(node, "shell")).strip() or shell):
            yield run
        for _, value in node.value:
            yield from _run_blocks(value, shell)


def _run_lines(lines: list[str], run: yaml.ScalarNode) -> list[int]:
    """List the file line of each refused loop in one run text.

    A literal or folded block is graded from its own lines in the file, indentation and all, so each loop names its
    line; a plain or quoted run text, whose lines YAML may fold or escape, names its first line.
    """
    block = run.style in ("|", ">")
    end = run.end_mark.line + bool(run.end_mark.column)
    body = "\n".join(lines[run.start_mark.line + 1 : end]) if block else run.value
    return [run.start_mark.line + 1 + (line if block else 0) for line in findings(blank_expressions(body).encode())]


def workflow_findings(text: str) -> list[int]:
    """List the file line of each refused for loop in the bash or sh run texts of a workflow or action, in order."""
    lines = text.splitlines()
    return sorted(
        line for run in _run_blocks(yaml.SafeLoader(text).get_single_node(), "bash") for line in _run_lines(lines, run)
    )


def _is_workflow_or_action(name: str) -> bool:
    """Tell whether a tracked file is a workflow under .github/workflows or an action definition with run steps."""
    path = PurePosixPath(name)
    return path.name in ACTIONS or (path.parent == WORKFLOWS and path.suffix in (".yml", ".yaml"))


def main(argv: Sequence[str] | None = None) -> int:
    """Grade every tracked shell file but the red fixture and every workflow and action, and report each refusal."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    root: Path = parser.parse_args(argv).root
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    names = [name for name in listed.stdout.decode().split("\0") if name]
    shells = [name for name in names if name != RED_FIXTURE and language(root, name) == "shell"]
    workflows = [name for name in names if _is_workflow_or_action(name)]
    graded = [(name, findings((root / name).read_bytes())) for name in shells]
    graded += [(name, workflow_findings((root / name).read_bytes().decode())) for name in workflows]
    problems = [f"{name}:{line}: {ADVICE}" for name, lines in graded for line in lines]
    if not shells:
        problems.append("No tracked shell file found; the check graded no shell script")
    if not workflows:
        problems.append("No tracked workflow file found; the check graded no run block")
    print(f"For loop word lists: {len(shells)} shell files, {len(workflows)} workflow files, {len(problems)} problems")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
