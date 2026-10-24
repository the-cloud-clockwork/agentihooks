import contextlib
import shlex
from collections.abc import MutableMapping
from pathlib import Path

KEY = "GH_CONFIG_DIR"
# gh 2.x renames the home gh folder into $XDG_CONFIG_HOME/gh when that folder is missing and GH_CONFIG_DIR is unset.
SHELL_PIN = (
    'if [ -n "${XDG_CONFIG_HOME:-}" ] && [ "$XDG_CONFIG_HOME" != "$HOME/.config" ]; then '
    'export GH_CONFIG_DIR="${GH_CONFIG_DIR:-$HOME/.config/gh}"; fi\n'
)


def pin(environ: MutableMapping[str, str]) -> None:
    default = Path(environ.get("HOME") or Path.home()) / ".config"
    isolated = environ.get("XDG_CONFIG_HOME")
    if isolated and Path(isolated) != default and not environ.get(KEY):
        environ[KEY] = str(default / "gh")


def persist(environ: MutableMapping[str, str]) -> None:
    target, pinned = environ.get("CLAUDE_ENV_FILE"), environ.get(KEY)
    if not (target and pinned):
        return
    path = Path(target)
    line = f"export {KEY}={shlex.quote(pinned)}\n"
    with contextlib.suppress(OSError, ValueError):
        current = path.read_text() if path.is_file() else ""
        if line not in current:
            with path.open("a") as handle:
                handle.write(("\n" if current and not current.endswith("\n") else "") + line)
