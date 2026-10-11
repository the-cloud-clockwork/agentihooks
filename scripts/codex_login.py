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

REFRESH_FLAG = aicode_tokens.REFRESH_FLAG
REFRESH_PREFIX = "AH_CX_REFRESH_"
PROG = "agentihooks codex login"
FILE_STORE = ["-c", 'cli_auth_credentials_store="file"']
UNKNOWN = "unknown"
EXPIRY_FORMAT = "%Y-%m-%d %H:%M UTC"
DESCRIBE = "{name} written to {rc}\n  workspace {workspace}, plan {plan}\n  expires {expires}\nrun: source {rc}"
ASK_STORE = "Do you want to store them in OpenBao?"
ASK_DETECTED = "OpenBao credentials detected ({url}). Use them?"
SKIP_OFF_TTY = "not a terminal: skipping OpenBao (pass --openbao to store the tokens there)"
NEEDS_ENV = f"--openbao needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} off a terminal"
NEEDS_BOTH = "OpenBao needs both a URL and a token"


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


def _fail(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("slug")
    parser.add_argument("--device-auth", action="store_true")
    store = parser.add_mutually_exclusive_group()
    store.add_argument("--openbao", action="store_true", default=None)
    store.add_argument("--no-openbao", dest="openbao", action="store_false")
    return parser


def _codex_login(home: Path, device_auth: bool, environ: Mapping[str, str], run: Callable) -> int:
    child = {**envs.codex_subscription_child(environ), "CODEX_HOME": str(home), INTERNAL_RUN: "1"}
    argv = [shutil.which("codex") or "codex", "login", *(["--device-auth"] if device_auth else []), *FILE_STORE]
    try:
        return run(argv, env=child).returncode
    except OSError as exc:
        _fail(f"cannot run codex: {exc.strerror or exc}")
        return 1


def _tokens(home: Path, name: str = "auth.json") -> dict:
    try:
        tokens = json.loads((home / name).read_bytes()).get("tokens")
    except (OSError, ValueError, AttributeError):
        return {}
    return tokens if isinstance(tokens, dict) else {}


def _login(slug: str, device_auth: bool, environ: Mapping[str, str], run: Callable) -> tuple[int, dict]:
    home = homes() / slug
    created = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    code = _codex_login(home, device_auth, environ, run)
    tokens = {} if code else _tokens(home)
    if not code and not tokens.get("access_token"):
        _fail(f"no tokens.access_token in {home / 'auth.json'}")
        code = 1
    if code and created:
        shutil.rmtree(home, ignore_errors=True)
    return code, {} if code else tokens


def _names(prefix: str, slugs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}{slug}" for slug in slugs)


def _describe(slug: str, access: str, rc: Path) -> None:
    claims = codex_router._claims(access)
    auth = claims.get(codex_router.OPENAI_AUTH_CLAIM) or {}
    exp = claims.get("exp")
    print(
        DESCRIBE.format(
            name=f"{CODEX_TOKEN_PREFIX}{slug}",
            rc=rc,
            workspace=codex_router._chatgpt_account_id(claims) or UNKNOWN,
            plan=auth.get("chatgpt_plan_type") or UNKNOWN,
            expires=time.strftime(EXPIRY_FORMAT, time.gmtime(exp)) if isinstance(exp, int) else UNKNOWN,
        )
    )


def _bao_target(choice: bool | None, environ: Mapping[str, str], console: Console, opener) -> openbao_kv.OpenBao | None:
    if choice is False:
        return None
    if choice is None and not console.tty():
        print(SKIP_OFF_TTY)
        return None
    if choice is None and not console.confirm(ASK_STORE, False):
        return None
    detected = openbao_kv.from_env(environ, opener)
    if detected and (choice or console.confirm(ASK_DETECTED.format(url=detected.url), True)):
        return detected
    if not console.tty():
        raise StoreRefused(NEEDS_ENV)
    url, token = console.ask("OpenBao URL: ").strip(), console.hidden("OpenBao token: ").strip()
    if not (url and token):
        raise StoreRefused(NEEDS_BOTH)
    return openbao_kv.OpenBao(url, token, opener)


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
        _fail(str(exc))
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
        _fail(str(exc))
        return 1
    if bao is None:
        return 0
    entries = {name: tokens["access_token"]}
    if tokens.get("refresh_token"):
        entries[f"{REFRESH_PREFIX}{slug}"] = tokens["refresh_token"]
    return _store(bao, entries, _names(CODEX_TOKEN_PREFIX, old) + _names(REFRESH_PREFIX, old))
