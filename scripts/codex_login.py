"""`agentihooks codex login <slug>`: one ChatGPT login in its own Codex home, exported as AH_CX_TOKEN_<slug>.

No token value is printed. Typed OpenBao credentials serve one write and are kept nowhere.
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hooks.context.account_sessions import CODEX_TOKEN_PREFIX, INTERNAL_RUN
from scripts import aicode_tokens, codex_router, openbao_kv
from scripts.routing import envs

REFRESH_FLAG = "AGENTIHOOKS_AICODE_TOKEN_REFRESH"
REFRESH_PREFIX = "AH_CX_REFRESH_"
FILE_STORE = ["-c", 'cli_auth_credentials_store="file"']


class StoreRefused(RuntimeError):
    pass


def homes() -> Path:
    return Path.home() / ".agentihooks" / "codex"


@dataclass(frozen=True)
class Console:
    ask: Callable[[str], str] = input
    hidden: Callable[[str], str] = getpass.getpass
    tty: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())

    def confirm(self, question: str, default: bool) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer.startswith("y")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks codex login")
    parser.add_argument("slug")
    parser.add_argument("--device-auth", action="store_true")
    store = parser.add_mutually_exclusive_group()
    store.add_argument("--openbao", dest="openbao", action="store_true", default=None)
    store.add_argument("--no-openbao", dest="openbao", action="store_false")
    return parser


def _codex_login(home: Path, device_auth: bool, environ: Mapping[str, str], run: Callable) -> int:
    child = {**envs.codex_subscription_child(environ), "CODEX_HOME": str(home), INTERNAL_RUN: "1"}
    argv = [shutil.which("codex") or "codex", "login", *(["--device-auth"] if device_auth else []), *FILE_STORE]
    return run(argv, env=child).returncode


def _tokens(home: Path) -> dict:
    try:
        tokens = json.loads((home / "auth.json").read_text(encoding="utf-8")).get("tokens")
    except (OSError, ValueError, AttributeError):
        return {}
    return tokens if isinstance(tokens, dict) else {}


def _login(slug: str, device_auth: bool, environ: Mapping[str, str], run: Callable) -> tuple[int, dict]:
    home = homes() / slug
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    code = _codex_login(home, device_auth, environ, run)
    if code:
        return code, {}
    tokens = _tokens(home)
    if not tokens.get("access_token"):
        print(f"agentihooks codex login: no tokens.access_token in {home / 'auth.json'}", file=sys.stderr)
        return 1, {}
    return 0, tokens


def _names(prefix: str, slugs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}{slug}" for slug in slugs)


def _describe(slug: str, access: str, rc: Path) -> None:
    claims = codex_router._claims(access)
    auth = claims.get(codex_router.OPENAI_AUTH_CLAIM) or {}
    exp = int(claims.get("exp") or 0)
    expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) if exp else "unknown"
    print(f"{CODEX_TOKEN_PREFIX}{slug} written to {rc}")
    print(f"  workspace {codex_router._chatgpt_account_id(claims) or 'unknown'}, plan {auth.get('chatgpt_plan_type')}")
    print(f"  expires {expires}")
    print(f"run: source {rc}")


def _typed(console: Console) -> tuple[str, str]:
    return console.ask("OpenBao URL: ").strip(), console.hidden("OpenBao token: ").strip()


def _bao_target(choice: bool | None, environ: Mapping[str, str], console: Console, opener) -> openbao_kv.OpenBao | None:
    if choice is False:
        return None
    if choice is None and not console.tty():
        print("not a terminal: skipping OpenBao (pass --openbao to store the tokens there)")
        return None
    if choice is None and not console.confirm("Do you want to store them in OpenBao?", False):
        return None
    detected = openbao_kv.from_env(environ, opener)
    if detected and (choice or console.confirm(f"OpenBao credentials detected ({detected.url}). Use them?", True)):
        return detected
    if not console.tty():
        raise StoreRefused(f"--openbao needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} off a terminal")
    url, token = _typed(console)
    if not (url and token):
        raise StoreRefused("OpenBao needs both a URL and a token")
    return openbao_kv.OpenBao(url, token, opener) if opener else openbao_kv.OpenBao(url, token)


def _store(bao: openbao_kv.OpenBao, entries: dict[str, str], stale: tuple[str, ...]) -> int:
    try:
        total = openbao_kv.merge(bao, openbao_kv.CODEX_CREDS, entries, stale)
    except openbao_kv.OpenBaoError as exc:
        print(f"OpenBao write failed: {exc}")
        return 1
    print(openbao_kv.summary(openbao_kv.CODEX_CREDS, entries, total))
    return 0


def main(
    args: list[str],
    environ: Mapping[str, str] | None = None,
    run: Callable = subprocess.run,
    console: Console | None = None,
    opener: Callable | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    options = _parser().parse_args(args)
    try:
        slug = aicode_tokens.slug(options.slug)
    except aicode_tokens.SlugError as exc:
        print(f"agentihooks codex login: {exc}", file=sys.stderr)
        return 2
    code, tokens = _login(slug, options.device_auth, environ, run)
    if code:
        return code
    old = () if options.slug == slug else (options.slug,)
    name = f"{CODEX_TOKEN_PREFIX}{slug}"
    rc = aicode_tokens.rc_file()
    aicode_tokens.write_rc(rc, {name: tokens["access_token"], REFRESH_FLAG: "true"}, _names(CODEX_TOKEN_PREFIX, old))
    _describe(slug, tokens["access_token"], rc)
    try:
        bao = _bao_target(options.openbao, environ, console or Console(), opener)
    except StoreRefused as exc:
        print(f"agentihooks codex login: {exc}", file=sys.stderr)
        return 1
    if bao is None:
        return 0
    entries = {name: tokens["access_token"]}
    if tokens.get("refresh_token"):
        entries[f"{REFRESH_PREFIX}{slug}"] = tokens["refresh_token"]
    return _store(bao, entries, _names(CODEX_TOKEN_PREFIX, old) + _names(REFRESH_PREFIX, old))
