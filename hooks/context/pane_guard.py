from pathlib import Path

from hooks.context.shell_commands import _SHELLS, commands
from hooks.hook_manager import BlockAction

DENY = (
    "BLOCKED [pane]: agents never open a herdr pane, tab, workspace or terminal on their own. "
    'Only the operator\'s own prompt this turn, typed in this terminal, opens one with the phrase "open pane". '
    "Put a step only the operator can do in Priorities, or write it in this terminal as a command for him to run."
)
_HERDR = {
    ("pane", "split"),
    ("pane", "run"),
    ("tab", "create"),
    ("workspace", "create"),
    ("worktree", "create"),
    ("worktree", "open"),
}
_HERDR_VALUED = {"--session", "--machine", "--remote", "--remote-keybindings"}
_AGENTIHOOKS = (["run-in-terminal"], ["init-agent"])
_SCRIPTS = {"02_run_terminal.sh", "open_param_pane.sh", "run_param_pane.sh"}


def _flag(session_id: str) -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.pane"


def set_pane_signal(session_id: str) -> None:
    flag = _flag(session_id)
    flag.parent.mkdir(exist_ok=True)
    flag.touch()


def observe_pane_signal(session_id: str, prompt: str, typed: bool) -> None:
    from hooks.context.ci_manifesto import contains_pane_signal

    if not session_id:
        return
    if typed and contains_pane_signal(prompt):
        set_pane_signal(session_id)
    else:
        clear_pane_signal(session_id)


def clear_pane_signal(session_id: str) -> None:
    _flag(session_id).unlink(missing_ok=True)


def has_pane_signal(session_id: str) -> bool:
    return bool(session_id) and _flag(session_id).exists()


def _script(tokens: list[str]) -> bool:
    if Path(tokens[0]).name in _SHELLS:
        tokens = [token for token in tokens[1:] if not token.startswith("-")]
    return any(Path(token).name in _SCRIPTS for token in tokens[:1])


def _herdr(args: list[str]) -> bool:
    words = []
    while args and len(words) < 2:
        token, args = args[0], args[1:]
        if token in _HERDR_VALUED:
            args = args[1:]
        elif not token.startswith("-"):
            words.append(token)
    return tuple(words) in _HERDR


def opens_pane(tokens: list[str]) -> bool:
    if not tokens:
        return False
    name = Path(tokens[0]).name
    if name == "herdr":
        return _herdr(tokens[1:])
    if name == "agentihooks":
        return tokens[1:2] in _AGENTIHOOKS or (tokens[1:2] == ["swarm"] and tokens[4:5] == ["up"])
    return _script(tokens)


def _quota_handoff(tokens: list[str], session_id: str) -> bool:
    if tokens[1:2] != ["init-agent"] or "--handoff" not in tokens or "--prompt-file" not in tokens[:-1]:
        return False
    document = Path(tokens[tokens.index("--prompt-file") + 1])
    if document.name != f"{session_id}.md" or document.parent.name != "handoff":
        return False
    from hooks.context.quota_policy import evaluate

    try:
        decision = evaluate(session_id)
    except Exception:
        return False
    return decision is not None and decision.action == "handoff"


def check_panes(payload: dict) -> None:
    session_id = payload.get("session_id")
    if payload.get("tool_name") != "Bash" or has_pane_signal(session_id):
        return
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd")
    if command and any(opens_pane(tokens) and not _quota_handoff(tokens, session_id) for tokens in commands(command)):
        raise BlockAction(DENY)
