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
