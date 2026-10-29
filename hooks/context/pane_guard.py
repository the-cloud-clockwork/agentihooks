from pathlib import Path

from hooks.context.shell_commands import _SHELLS

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
