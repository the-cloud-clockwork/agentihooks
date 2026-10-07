"""The env files the operator's shell loads through the agentienv block, for processes that never ran that shell."""

import shlex
import subprocess
from collections.abc import Mapping, MutableMapping
from pathlib import Path

SHELL_NAMES = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})
SOURCE = 'set -a; for f in "$@"; do . "$f" 2>/dev/null; done; set +a'


def files(environ: Mapping[str, str]) -> list[Path]:
    home = Path(environ.get("HOME") or Path.home())
    state = Path(environ.get("AGENTIHOOKS_HOME") or home / ".agentihooks")
    main = state / ".env"
    found = []
    if main.is_file():
        companions = (path for path in sorted(state.glob("*.env")) if not path.name.startswith("."))
        found = [main, *(path for path in companions if path.is_file())]
    personal = home / ".env"
    return [*found, *([personal] if personal.is_file() else [])]


def values(environ: Mapping[str, str]) -> dict[str, str]:
    script = source_line(environ)
    if not script:
        return {}
    base = {key: environ[key] for key in ("HOME", "PATH", "AGENTIHOOKS_HOME") if environ.get(key)}
    done = subprocess.run(["bash", "--noprofile", "--norc", "-c", f"{script}env -0"], env=base, capture_output=True)
    pairs = (item.decode(errors="surrogateescape").partition("=") for item in done.stdout.split(b"\0") if item)
    return {key: value for key, _, value in pairs if key not in SHELL_NAMES}


def fill(environ: MutableMapping[str, str]) -> list[str]:
    loaded = values(environ)
    added = [key for key in loaded if key not in environ]
    environ.update({key: loaded[key] for key in added})
    return added


def source_line(environ: Mapping[str, str]) -> str:
    paths = files(environ)
    return f"set -- {shlex.join(map(str, paths))}; {SOURCE}; set --\n" if paths else ""
