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

_SHORT_FLAGS = re.compile(r"-[A-Za-z]+")
_REVIEWS = re.compile(r"pulls/[^/]+/reviews\b")
_GRAPHQL = re.compile(r"(?:^|/)graphql$")
_MUTATION = re.compile(r"\b(?:add|submit)PullRequestReview\b")
_FIELD = re.compile(r"(?i)^(?:--(?:raw-)?field=|-f)?event=(.*)$")
_WORDS = re.compile(r"\w+")
_BODY = re.compile(r"""(?i)\\?["']event\\?["']\s*:\s*\\?["']approve\\?["']""")
_INLINE = re.compile(r"(?i)\bevent\s*:\s*approve\b")
_VARIABLE = re.compile(r"\bevent\s*:\s*\$(\w+)")
_FILE = re.compile(r"^(?:--input=(.+)|(?:[^=@]*=|-[A-Za-z])?@(.+))")


def _subcommand(words: list[str]) -> list[str]:
    """Return a gh command's first two words that are neither an option nor the repository -R or --repo names."""
    pairs = zip(words, words[1:])
    return [word for before, word in pairs if not word.startswith("-") and before not in ("-R", "--repo")][:2]


def _read(paths: list[Path]) -> list[str]:
    """Return the text of each path that is a regular file."""
    return [path.read_bytes().decode("latin-1") for path in paths if path.is_file()]


def _files(words: list[str], cwd: str) -> list[str]:
    """Return the text of each regular file a call reads its body or a field from: the --input file, and a field or
    a curl data option whose value starts with @."""
    names = [after for before, after in zip(words, words[1:]) if before == "--input"]
    names += [found[1] or found[2] for found in map(_FILE.search, words) if found]
    return _read([Path(cwd, name) for name in names])


def _events(words: list[str], command: str, cwd: str) -> list[str]:
    """Return the value of each event field a call sets, the text of each file a value given as @file names, and every
    word of the command line when a value given as @- reads the event from stdin."""
    values = [found[1] for found in map(_FIELD.match, words) if found]
    stdin = _WORDS.findall(command) if "@-" in values else []
    return values + stdin + _read([Path(cwd, value[1:]) for value in values if value.startswith("@")])


def _text(words: list[str], command: str, cwd: str) -> str:
    """Return the text one call sends: its own words, the files it reads, and the whole command line when it reads
    its body from stdin, which a heredoc or an earlier pipeline stage supplies."""
    reads_stdin = any(word == "-" or word.endswith(("=-", "@-")) for word in words)
    stdin = [command] if reads_stdin else []
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
    approves = any(value.strip().upper() == "APPROVE" for value in _events(words, command, cwd))
    return reviews and (approves or _BODY.search(text) is not None)


def _call(words: list[str]) -> list[str]:
    """Return a simple command from its first gh or curl word on, named by its program, so xargs, find -exec and other
    launchers are judged on the call they run; empty when neither program runs."""
    for at, word in enumerate(words):
        if Path(word).name in ("gh", "curl"):
            return [Path(word).name, *words[at + 1 :]]
    return []


def _approves(words: list[str], command: str, cwd: str) -> bool:
    """Return whether one simple command approves a pull request through gh pr review, a review endpoint or the
    GraphQL review mutations."""
    call = _call(words)
    if call[:1] == ["gh"] and _subcommand(call) == ["pr", "review"]:
        return any(
            word.partition("=")[0] == "--approve" or (_SHORT_FLAGS.fullmatch(word) and "a" in word) for word in call
        )
    if call[:1] == ["curl"] or (call[:1] == ["gh"] and _subcommand(call)[:1] == ["api"]):
        return _api_approves(call, command, cwd)
    return False


def _bash_approves(command: str, cwd: str) -> bool:
    """Return whether a shell command approves a pull request; a command the shell guards cannot parse is refused when
    its text names both a review and an approval."""
    try:
        found = commands(command)
    except ValueError:
        lowered = command.lower()
        return "review" in lowered and "approve" in lowered
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
    from hooks.hook_manager import BlockAction

    if reason := refusal(payload):
        raise BlockAction(reason)
