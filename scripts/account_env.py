"""`agentihooks accounts env`: one export line per Claude and Codex account token, for a shell to evaluate.

Tokens come from the OpenBao secret each harness's accounts share, else from an owner only fallback file;
they are printed, never written anywhere. Codex refresh tokens are never exported: only the refresh
authority rotates them.
"""

import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Callable, Mapping
from http.client import HTTPException
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts import openbao_kv
from scripts.account_names import CODEX_TOKEN_PREFIX, TOKEN_PREFIX, codex_token_env

TIMEOUT_S = 3
USAGE = "usage: agentihooks accounts env"


class Accounts(NamedTuple):
    prefix: str
    fallback: str
    path: Callable[[Mapping[str, str]], str]
    from_email: Callable[[str], str] | None


CLAUDE = Accounts(TOKEN_PREFIX, "claude.env", lambda environ: openbao_kv.CLAUDE_CREDS, None)
CODEX = Accounts(CODEX_TOKEN_PREFIX, "codex.env", openbao_kv.codex_creds, codex_token_env)


def _named(name: str, prefix: str) -> bool:
    return bool(re.fullmatch(rf"{prefix}[a-z0-9_]+", name))


def _exported(secret: Mapping[str, object], accounts: Accounts) -> dict[str, str]:
    values = {key: value for key, value in secret.items() if isinstance(value, str) and value}
    by_email = {accounts.from_email(key): value for key, value in values.items() if accounts.from_email and "@" in key}
    return by_email | {key: value for key, value in values.items() if _named(key, accounts.prefix)}


class Unreachable(Exception):
    pass


def from_openbao(environ: Mapping[str, str], accounts: Accounts) -> dict[str, str] | None:
    url, bao, path = environ.get("OPENBAO_URL"), environ.get("OPENBAO_TOKEN"), accounts.path(environ)
    if not url or not bao or not path:
        return None
    request = Request(f"{url.removesuffix('/')}/v1/{openbao_kv.MOUNT}/data/{path}", headers={"X-Vault-Token": bao})
    try:
        with urlopen(request, timeout=TIMEOUT_S) as response:
            tokens = _exported(json.load(response)["data"]["data"], accounts)
    except (HTTPError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return None
    except OSError as error:
        raise Unreachable from error
    return tokens or None


def fallback_path(environ: Mapping[str, str], name: str) -> Path:
    home = Path(environ.get("HOME") or Path.home())
    return Path(environ.get("AGENTIHOOKS_HOME") or home / ".agentihooks") / "accounts" / name


def parse(text: str, prefix: str) -> dict[str, str]:
    tokens = {}
    for line in text.splitlines():
        name, sep, raw = line.strip().removeprefix("export ").partition("=")
        try:
            words = shlex.split(raw)
        except ValueError:
            continue
        if sep and _named(name, prefix) and len(words) == 1:
            tokens[name] = words[0]
    return tokens


def from_file(path: Path, prefix: str) -> tuple[dict[str, str], str]:
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return {}, f"{path} is readable by others; chmod 600 it"
        return parse(path.read_bytes().decode(), prefix), f"accounts from {path}"
    except OSError:
        return {}, f"no fallback at {path}"


def load(environ: Mapping[str, str], accounts: Accounts, reachable: bool = True) -> tuple[dict[str, str], bool]:
    try:
        tokens = from_openbao(environ, accounts) if reachable else None
    except Unreachable:
        tokens, reachable = None, False
    if tokens is not None:
        return tokens, reachable
    fallback = fallback_path(environ, accounts.fallback)
    tokens, note = from_file(fallback, accounts.prefix)
    if fallback.exists() or (environ.get("OPENBAO_URL") and accounts.path(environ)):
        print(f"agentihooks accounts: OpenBao unreachable, {note}", file=sys.stderr)
    return tokens, reachable


def render(tokens: Mapping[str, str]) -> str:
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in sorted(tokens.items()))


def main(argv: list[str]) -> int:
    if argv != ["env"]:
        print(USAGE, file=sys.stderr)
        return 2
    claude, reachable = load(os.environ, CLAUDE)
    codex, _ = load(os.environ, CODEX, reachable)
    sys.stdout.write(render({**claude, **codex}))
    return 0
