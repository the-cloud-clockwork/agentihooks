"""The env files the operator's shell loads through the agentienv block, for processes that never ran that shell."""

import json
import shlex
import subprocess
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path

from scripts import aicode_tokens

SHELL_NAMES = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})
SOURCE = 'set -a; for f in "$@"; do . "$f" 2>/dev/null; done; set +a'
ACCOUNT_PREFIX = "AH_CC_TOKEN_"
ACCOUNTS_MARK = "agentihooks-accounts"
CLASSIFIER_PREFIX = "AGENTIHOOKS_CLASSIFIER_"
CLASSIFIER_NEEDS = ("AGENTIHOOKS_CLASSIFIER_URL", "AGENTIHOOKS_CLASSIFIER_LITELLM_KEY")


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
    return {key: value for key, _, value in pairs if key not in SHELL_NAMES and key != "SWARM_SECRET"}


def fill(environ: MutableMapping[str, str]) -> list[str]:
    loaded = values(environ)
    added = [key for key in loaded if key not in environ]
    environ.update({key: loaded[key] for key in added})
    block = block_tokens()
    renewed = [key for key, value in block.items() if environ.get(key) != value and key not in added]
    environ.update(block)
    return added + renewed


def block_tokens() -> dict[str, str]:
    try:
        text = aicode_tokens.rc_file().read_bytes().decode()
    except (OSError, UnicodeDecodeError):
        return {}
    return aicode_tokens.token_exports(text)


def distinct(environ: Mapping[str, str], names: Iterable[str]) -> list[str]:
    block = block_tokens()
    chosen: dict[str, str] = {}
    for name in sorted(names):
        value = environ[name]
        held = chosen.get(value)
        if held is None or (block.get(name) == value and block.get(held) != value):
            chosen[value] = name
    return sorted(chosen.values())


def source_line(environ: Mapping[str, str]) -> str:
    paths = files(environ)
    return f"set -- {shlex.join(map(str, paths))}; {SOURCE}; unset SWARM_SECRET; set --\n" if paths else ""


def _shell_exports(environ: Mapping[str, str], prefix: str) -> dict[str, str]:
    script = f"printf '\\0{ACCOUNTS_MARK}\\0'; env -0"
    done = subprocess.run(["bash", "-lic", script], env=dict(environ), stdin=subprocess.DEVNULL, capture_output=True)
    _, marked, listing = done.stdout.rpartition(f"\0{ACCOUNTS_MARK}\0".encode())
    if not marked:
        return {}
    pairs = (item.decode(errors="surrogateescape").partition("=") for item in listing.split(b"\0") if item)
    return {key: value for key, _, value in pairs if key.startswith(prefix)}


def accounts(environ: Mapping[str, str]) -> dict[str, str]:
    return _shell_exports(environ, ACCOUNT_PREFIX)


def _claude_settings_env(environ: Mapping[str, str], prefix: str) -> dict[str, str]:
    path = Path(environ.get("HOME") or Path.home()) / ".claude" / "settings.json"
    try:
        env = json.loads(path.read_text()).get("env")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(env, dict):
        return {}
    return {key: value for key, value in env.items() if key.startswith(prefix) and isinstance(value, str) and value}


def classifier(environ: MutableMapping[str, str]) -> list[str]:
    found = _claude_settings_env(environ, CLASSIFIER_PREFIX)
    if not all(environ.get(name) or found.get(name) for name in CLASSIFIER_NEEDS):
        found = {**_shell_exports(environ, CLASSIFIER_PREFIX), **found}
    added = sorted(key for key, value in found.items() if value and not environ.get(key))
    environ.update({key: found[key] for key in added})
    return added
