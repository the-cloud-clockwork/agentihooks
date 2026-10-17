from pathlib import Path

from hooks.context.shell_commands import _SHELLS, commands
from hooks.hook_manager import BlockAction

DENY = (
    "BLOCKED [pane]: agents never open a herdr pane, tab, workspace or terminal on their own. "
    'Only the operator\'s own prompt this turn, typed in this terminal, opens one with the phrase "open pane". '
    "Put a step only the operator can do in Priorities, or write it in this terminal as a command for him to run."
)
_HERDR = {"pane": {"split", "run"}, "tab": {"create"}, "workspace": {"create"}, "worktree": {"create", "open"}}
_HERDR_VALUED = {"--session", "--machine", "--remote"}
_AGENTIHOOKS = {"run-in-terminal", "init-agent"}
_SCRIPTS = (
    "run-in-terminal/scripts/02_run_terminal.sh",
    "prompt-user-parameter/scripts/open.sh",
    "prompt-user-parameter/scripts/run.sh",
)


def _flag(session_id: str) -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.pane"


def set_pane_signal(session_id: str) -> None:
    flag = _flag(session_id)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("1")


def observe_pane_signal(session_id: str, prompt: str) -> None:
    from hooks.context.ci_manifesto import contains_pane_signal

    if session_id and contains_pane_signal(prompt):
        set_pane_signal(session_id)


def clear_pane_signal(session_id: str) -> None:
    _flag(session_id).unlink(missing_ok=True)


def has_pane_signal(session_id: str) -> bool:
    return bool(session_id) and _flag(session_id).exists()


def _herdr(args: list[str]) -> bool:
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in _HERDR_VALUED else args[1:]
    return len(args) > 1 and args[1] in _HERDR.get(args[0], ())


def _script(tokens: list[str]) -> bool:
    if Path(tokens[0]).name in _SHELLS:
        tokens = [token for token in tokens[1:] if not token.startswith("-")] or [""]
    return tokens[0].endswith(_SCRIPTS)


def opens_pane(tokens: list[str]) -> bool:
    if not tokens:
        return False
    name = Path(tokens[0]).name
    if name == "herdr":
        return _herdr(tokens[1:])
    if name == "agentihooks":
        return (
            len(tokens) > 1 and tokens[1] in _AGENTIHOOKS and not (tokens[1] == "init-agent" and "--handoff" in tokens)
        )
    return _script(tokens)


def check_panes(payload: dict) -> None:
    if payload.get("tool_name") != "Bash" or has_pane_signal(payload.get("session_id", "")):
        return
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd")
    if command and any(opens_pane(tokens) for tokens in commands(command)):
        raise BlockAction(DENY)
