import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from hooks.context.account_sessions import CODEX_TOKEN_PREFIX, INTERNAL_RUN
from scripts import aicode_tokens, codex_router, openbao_kv, openbao_step, term_colors
from scripts.openbao_step import Console, StoreRefused
from scripts.routing import envs

REFRESH_FLAG = aicode_tokens.REFRESH_FLAG
REFRESH_PREFIX = "AH_CX_REFRESH_"
PROG = "agentihooks codex login"
FILE_STORE = ["-c", 'cli_auth_credentials_store="file"']
UNKNOWN = "unknown"
EXPIRY_FORMAT = "%Y-%m-%d %H:%M UTC"
DESCRIBE = "{name} written to {rc}\n  workspace {workspace}, plan {plan}\n  expires {expires}\nrun: source {rc}"
STEP_SIGN_IN = "Step 1/3: sign in to the account in the browser"
STEP_BLOCK = "Step 2/3: writing the token block"
STEP_STORE = "Step 3/3: storing them in OpenBao"


def homes() -> Path:
    return Path.home() / ".agentihooks" / "codex"


def _fail(message: str) -> None:
    print(term_colors.red(f"{PROG}: {message}"), file=sys.stderr)


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
    print(term_colors.blue(STEP_SIGN_IN))
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


def main(
    args: list[str],
    environ: Mapping[str, str] | None = None,
    run: Callable = subprocess.run,
    console: Console | None = None,
    opener: Callable | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    options = _parser().parse_args(args)
    print(term_colors.blue(aicode_tokens.SLUG_NOTE.format(prefix=CODEX_TOKEN_PREFIX)))
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
    print(term_colors.blue(STEP_BLOCK))
    aicode_tokens.write_rc(rc, {name: tokens["access_token"], REFRESH_FLAG: "true"}, _names(CODEX_TOKEN_PREFIX, old))
    _describe(slug, tokens["access_token"], rc)
    try:
        bao = openbao_step.target(options.openbao, environ, console or Console(), opener)
    except StoreRefused as exc:
        _fail(str(exc))
        return 1
    if bao is None:
        return 0
    entries = {name: tokens["access_token"]}
    if tokens.get("refresh_token"):
        entries[f"{REFRESH_PREFIX}{slug}"] = tokens["refresh_token"]
    print(term_colors.blue(STEP_STORE))
    stale = _names(CODEX_TOKEN_PREFIX, old) + _names(REFRESH_PREFIX, old)
    return openbao_step.store(bao, openbao_kv.CODEX_CREDS, entries, stale)
