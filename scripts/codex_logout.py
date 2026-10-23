import argparse
import os
import shutil
import sys
from collections.abc import Callable, Mapping

from hooks.context.account_sessions import CODEX_TOKEN_PREFIX
from scripts import aicode_tokens, codex_login, openbao_kv, openbao_step, term_colors
from scripts.openbao_step import Console, StoreRefused

PROG = "agentihooks codex logout"


def _fail(message: str) -> None:
    print(term_colors.red(f"{PROG}: {message}"), file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("slug")
    store = parser.add_mutually_exclusive_group()
    store.add_argument("--openbao", action="store_true", default=None)
    store.add_argument("--no-openbao", dest="openbao", action="store_false")
    return parser


def _forget(bao: openbao_kv.OpenBao, path: str, names: tuple[str, ...]) -> bool:
    try:
        left = openbao_kv.merge(bao, path, {}, names)
    except openbao_kv.OpenBaoError as exc:
        print(term_colors.red(f"OpenBao write failed: {exc}"))
        return False
    print(f"OpenBao {openbao_kv.MOUNT}/{path}: removed {', '.join(sorted(names))} ({left} keys left)")
    return True


def main(
    args: list[str],
    environ: Mapping[str, str] | None = None,
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
    slugs = tuple(dict.fromkeys((slug, options.slug)))
    tokens = tuple(f"{CODEX_TOKEN_PREFIX}{each}" for each in slugs)
    refresh = tuple(f"{codex_login.REFRESH_PREFIX}{each}" for each in slugs)
    try:
        bao = openbao_step.target(options.openbao, environ, console or Console(), opener)
    except StoreRefused as exc:
        _fail(str(exc))
        return 1
    if bao is None:
        print("OpenBao skipped: its Codex record still holds this account")
    elif not _forget(bao, openbao_kv.codex_creds(environ), tokens + refresh):
        return 1
    home = codex_login.homes() / slug
    if home.is_dir():
        shutil.rmtree(home)
        print(f"removed {home}")
    else:
        print(f"no account home at {home}")
    rc = aicode_tokens.rc_file()
    if aicode_tokens.remove_exports(rc, tokens):
        print(f"removed {', '.join(tokens)} from {rc}\nrun: unset {' '.join(tokens)}")
    else:
        print(f"no export of {', '.join(tokens)} in the token block of {rc}")
    return 0
