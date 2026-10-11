"""`agentihooks accounts env`: one export line per Claude account token, for a shell to evaluate.

Tokens come from the OpenBao secret every account shares with the swarm, else from an
owner only fallback file; they are printed, never written anywhere.
"""

import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.request import Request, urlopen

from scripts.account_names import TOKEN_PREFIX, token_env

SECRET_PATH = "secret/data/k8s/swarm-claude-creds"
FALLBACK_NAME = Path("accounts") / "claude.env"
TIMEOUT_S = 3
USAGE = "usage: agentihooks accounts env"
NAME = re.compile(rf"{TOKEN_PREFIX}[A-Za-z0-9_]+")


def from_openbao(environ: Mapping[str, str]) -> dict[str, str] | None:
    url, bao = environ.get("OPENBAO_URL"), environ.get("OPENBAO_TOKEN")
    if not url or not bao:
        return None
    request = Request(f"{url.removesuffix('/')}/v1/{SECRET_PATH}", headers={"X-Vault-Token": bao})
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:
            accounts = json.load(response)["data"]["data"]
        tokens = {token_env(email): value for email, value in accounts.items() if isinstance(value, str) and value}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return tokens or None


def fallback_path(environ: Mapping[str, str]) -> Path:
    home = Path(environ.get("HOME") or Path.home())
    return Path(environ.get("AGENTIHOOKS_HOME") or home / ".agentihooks") / FALLBACK_NAME


def parse(text: str) -> dict[str, str]:
    tokens = {}
    for line in text.splitlines():
        name, sep, raw = line.strip().removeprefix("export ").partition("=")
        try:
            words = shlex.split(raw)
        except ValueError:
            continue
        if sep and NAME.fullmatch(name) and len(words) == 1:
            tokens[name] = words[0]
    return tokens


def from_file(path: Path) -> tuple[dict[str, str], str]:
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return {}, f"{path} is readable by others; chmod 600 it"
        return parse(path.read_bytes().decode()), f"accounts from {path}"
    except OSError:
        return {}, f"no fallback at {path}"


def render(tokens: Mapping[str, str]) -> str:
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in sorted(tokens.items()))


def main(argv: list[str]) -> int:
    if argv != ["env"]:
        print(USAGE, file=sys.stderr)
        return 2
    tokens = from_openbao(os.environ)
    if tokens is None:
        path = fallback_path(os.environ)
        tokens, note = from_file(path)
        if path.exists() or os.environ.get("OPENBAO_URL"):
            print(f"agentihooks accounts: OpenBao unreachable, {note}", file=sys.stderr)
    sys.stdout.write(render(tokens))
    return 0
