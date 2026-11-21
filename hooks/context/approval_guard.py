"""Refuses every pull request approval an agent session runs, so a code owner approval comes from the operator.

The test count floor takes a code owner approval of the pull request head as the operator's review, and agent
sessions hold the operator's GitHub login. Bash runs of gh pr review with its approve flag, gh api or curl review
calls with an APPROVE event, and GitHub MCP tools with an APPROVE event are refused; comments and change requests pass.
"""

import re
from pathlib import Path
from typing import Any

from hooks.context.shell_commands import commands

REFUSAL = (
    "BLOCKED [pull request approval]: an agent session never approves a pull request. The test count floor takes a "
    "code owner approval of the head as the operator's own review, so only the operator approves, in GitHub. Leave a "
    "comment or request changes instead."
)

_SHORT_APPROVE = re.compile(r"-[A-Za-z]*a[A-Za-z]*")
_REVIEWS = re.compile(r"pulls/[^/]+/reviews\b")
_GRAPHQL = re.compile(r"(?:^|/)graphql$")
_MUTATION = re.compile(r"\b(?:add|submit)PullRequestReview\b")
_FIELD = re.compile(r"(?i)^(?:--(?:raw-)?field=|-[fF])?event=approve$")
_BODY = re.compile(r"""(?i)\\?["']event\\?["']\s*:\s*\\?["']approve\\?["']""")
_INLINE = re.compile(r"(?i)\bevent\s*:\s*approve\b")
_VARIABLE = re.compile(r"\bevent\s*:\s*\$(\w+)")
_FILE = re.compile(r"^(?:--input=(.+)|(?:.*?=)?@(.+))")
_STDIN = frozenset({"-", "@-"})


def _subcommand(words: list[str]) -> list[str]:
    """Return a gh command's first two words that are neither an option nor the repository -R or --repo names."""
    pairs = zip(words, words[1:])
    return [word for before, word in pairs if not word.startswith("-") and before not in ("-R", "--repo")][:2]


def _files(words: list[str], cwd: str) -> list[str]:
    """Return the text of each regular file a call reads its body or a field from: the --input file, and a field or
    a curl data option whose value starts with @."""
    names = [after for before, after in zip(words, words[1:]) if before == "--input"]
    names += [found[1] or found[2] for found in map(_FILE.search, words) if found]
    paths = [Path(cwd, name) for name in names]
    return [path.read_bytes().decode("latin-1") for path in paths if path.is_file()]


def _text(words: list[str], command: str, cwd: str) -> str:
    """Return the text one call sends: its own words, the files it reads, and the whole command line when it reads
    its body from stdin, which a heredoc or an earlier pipeline stage supplies."""
    stdin = [command] if _STDIN.intersection(words) else []
    return "\n".join([*words, *_files(words, cwd), *stdin])


def _graphql_approves(text: str) -> bool:
    """Return whether a GraphQL review mutation sets an APPROVE event inline or through the variable its event names."""
    if _INLINE.search(text):
        return True
    names = _VARIABLE.findall(text)
    return any(re.search(rf"""(?i)(?:\b{name}=|["']{name}["']\s*:\s*["'])approve\b""", text) for name in names)


def _api_approves(words: list[str], command: str, cwd: str) -> bool:
    """Return whether a gh api or curl call submits an approving review through a REST reviews endpoint or a GraphQL
    review mutation."""
    text = _text(words, command, cwd)
    if any(_GRAPHQL.search(word) for word in words) and _MUTATION.search(text):
        return _graphql_approves(text)
    reviews = any(_REVIEWS.search(word) for word in words)
    return reviews and (any(_FIELD.match(word) for word in words) or _BODY.search(text) is not None)


def _approves(words: list[str], command: str, cwd: str) -> bool:
    """Return whether one simple command approves a pull request through gh pr review, a review endpoint or the
    GraphQL review mutations."""
    program = Path(words[0]).name
    if program == "gh" and _subcommand(words) == ["pr", "review"]:
        return any(word.partition("=")[0] == "--approve" or _SHORT_APPROVE.fullmatch(word) for word in words)
    if program == "curl" or (program == "gh" and _subcommand(words)[:1] == ["api"]):
        return _api_approves(words, command, cwd)
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
        approves = _bash_approves(str(tool_input.get("command")), str(payload.get("cwd")))
    else:
        approves = "github" in tool_name.lower() and str(tool_input.get("event")).upper() == "APPROVE"
    return REFUSAL if approves else None


def check_approval(payload: dict[str, Any]) -> None:
    """Raise BlockAction for a tool call that approves a pull request."""
    from hooks.hook_manager import BlockAction

    if reason := refusal(payload):
        raise BlockAction(reason)
