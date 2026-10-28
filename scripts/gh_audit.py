"""Charge every gh REST request a process or an agent shell starts, so the sampler can attribute the whole spend.

GraphQL commands are left out: they draw on a separate limit from the core one the sampler measures.
"""

import shlex
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

REST_GROUPS = frozenset({"cache", "release", "run", "secret", "variable", "workflow"})
REST_PULL = frozenset({"diff"})
UNCHARGED = ContextVar("gh_uncharged", default=False)
SEPARATORS = frozenset("|&;()")
FREE = frozenset({"graphql", "rate_limit"})
VALUED = frozenset(
    {"-F", "-H", "-R", "-X", "-f", "-p", "-q", "-t", "--cache", "--field", "--header", "--hostname", "--input"}
    | {"--jq", "--method", "--preview", "--raw-field", "--repo", "--template"}
)


def endpoint(argv) -> str | None:
    words = [str(word) for word in argv]
    if not words or Path(words[0]).name != "gh":
        return None
    rest, skip = [], False
    for word in words[1:]:
        if not (skip or word.startswith("-")):
            rest.append(word)
        skip = not skip and word in VALUED
    if len(rest) < 2:
        return None
    group, verb = rest[0], rest[1]
    if group == "api":
        path = verb.split("?")[0]
        return None if path in FREE else path
    if group in REST_GROUPS or (group == "pr" and verb in REST_PULL):
        return f"gh {group} {verb}"
    return None


def process_caller() -> str:
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = spec.name if spec else Path(sys.argv[0] if sys.argv else "python").name
    args = [arg for arg in sys.argv[1:2] if not arg.startswith("-")]
    return ".".join(["proc", name.removesuffix(".__main__"), *args])


def charge(caller: str, path: str) -> None:
    from scripts.swarm import github_reads

    github_reads.charge(caller, path, time.time())


@contextmanager
def uncharged():
    """Around a run its caller charges itself, so the request is not charged twice."""
    token = UNCHARGED.set(True)
    try:
        yield
    finally:
        UNCHARGED.reset(token)


def audit(event: str, args: tuple) -> None:
    if event != "subprocess.Popen" or UNCHARGED.get():
        return
    path = endpoint(args[1] if isinstance(args[1], list | tuple) else [args[0] or ""])
    if path:
        from scripts.swarm import github_reads

        caller = github_reads.CALLER.get()
        charge(process_caller() if caller == github_reads.CALLER.default else caller, path)


def commands(text: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    found, current = [], []
    for word in words:
        if set(word) <= SEPARATORS:
            found.append(current)
            current = []
        else:
            current.append(word)
    return [command for command in [*found, current] if command]


def charge_command(tool_name: str, tool_input) -> None:
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return
    for command in commands(str(tool_input.get("command") or "")):
        path = endpoint(command)
        if path:
            charge("agent.bash", path)


def install() -> None:
    if not getattr(sys, "_agentihooks_gh_audit", False):
        sys._agentihooks_gh_audit = True
        sys.addaudithook(audit)
