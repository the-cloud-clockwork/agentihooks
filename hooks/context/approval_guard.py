"""Refuses every pull request approval an agent session runs, so a code owner approval comes from the operator.

The test count floor takes a code owner approval of the pull request head as the operator's review, and agent
sessions hold the operator's GitHub login. Bash runs of gh pr review with its approve flag, gh api or curl review
calls with an APPROVE event, and GitHub MCP tools with an APPROVE event are refused; comments and change requests pass.
"""

import re
from pathlib import Path
from typing import Any

from hooks.context.shell_commands import commands
from hooks.hook_manager import BlockAction

REFUSAL = (
    "BLOCKED [pull request approval]: an agent session never approves a pull request. The test count floor takes a "
    "code owner approval of the head as the operator's own review, so only the operator approves, in GitHub. Leave a "
    "comment or request changes instead."
)

_SHORT_APPROVE = re.compile(r"-[^-]*a")
_REVIEWS = re.compile(r"pulls/[^/]+/reviews\b")
_GRAPHQL = re.compile(r"(?:^|/)graphql$")
_MUTATION = re.compile(r"\b(?:add|submit)PullRequestReview\b")
_APPROVE = re.compile(r"""(?i:\bevent\b["']?\s*[:=]\s*["']?approve\b)|=APPROVE\b""")
_FILE = re.compile(r"^--input=(.+)|(?:^|=)@(.+)")


def _subcommand(words: list[str]) -> list[str]:
    """Return a gh command's first two words that are neither an option nor the repository -R or --repo names."""
    found: list[str] = []
    skip = False
    for word in words[1:]:
        if not skip and not word.startswith("-"):
            found.append(word)
        skip = word in ("-R", "--repo")
    return found[:2]


def _files(words: list[str], cwd: str) -> list[str]:
    """Return the text of each regular file a call reads its body or a field from: the --input file, and a field or
    a curl data option whose value starts with @."""
    names = [after for before, after in zip(words, words[1:]) if before == "--input"]
    names += [found[1] or found[2] for found in map(_FILE.search, words) if found]
    paths = [Path(cwd, name) for name in names]
    return [path.read_bytes().decode("latin-1") for path in paths if path.is_file()]


def _approves(words: list[str], command: str, cwd: str) -> bool:
    """Return whether one simple command approves a pull request through gh pr review, a review endpoint or the
    GraphQL review mutations."""
    program = Path(words[0]).name
    if program == "gh" and _subcommand(words) == ["pr", "review"]:
        return any(word.partition("=")[0] == "--approve" or _SHORT_APPROVE.match(word) for word in words)
    if program == "curl" or (program == "gh" and _subcommand(words)[:1] == ["api"]):
        text = "\n".join([command, *_files(words, cwd)])
        reviews = any(_REVIEWS.search(word) for word in words)
        graphql = any(_GRAPHQL.search(word) for word in words) and _MUTATION.search(text) is not None
        return (reviews or graphql) and _APPROVE.search(text) is not None
    return False


def _bash_approves(command: str, cwd: str) -> bool:
    """Return whether a shell command approves a pull request; a command the shell cannot parse runs nothing."""
    try:
        found = commands(command)
    except ValueError:
        return False
    return any(_approves(words, command, cwd) for words in found)


def refusal(payload: dict[str, Any]) -> str | None:
    """Return the refusal for a tool call that approves a pull request, or None for any other call."""
    tool_name = str(payload.get("tool_name"))
    tool_input: dict[str, Any] = payload.get("tool_input") or {}
    if tool_name == "Bash":
        approves = _bash_approves(str(tool_input.get("command")), str(payload.get("cwd") or ""))
    else:
        approves = "github" in tool_name.lower() and str(tool_input.get("event")).upper() == "APPROVE"
    return REFUSAL if approves else None


def check_approval(payload: dict[str, Any]) -> None:
    """Raise BlockAction for a tool call that approves a pull request."""
    if reason := refusal(payload):
        raise BlockAction(reason)
