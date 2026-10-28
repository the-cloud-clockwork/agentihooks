"""Standalone Claude sessions on the stored claude ai logins: a slot's credentials, never its claim."""

import os
import time
from collections.abc import Mapping
from pathlib import Path

from scripts import aicode_tokens, front_door_route, master_logins, openbao_kv, quota_state
from scripts import master_claude_login as login
from scripts.claude_config import claude_home, claude_json
from scripts.profiles import binding
from scripts.routing import native_login

ROUTE = "claude-ai"
STORE = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
FOLDER = "claude-ai"
LINKED = ("projects", "sessions")
EMPTY = "no claude ai login is stored: run agentihooks claude login --master"
NONE = "no usable claude ai login: {reasons}"
SPENT = "{account} has {left:.0f}% left"


def _candidates(bao: openbao_kv.OpenBao | None) -> list[tuple[int, str, Mapping[str, str]]]:
    offered = login._offered(bao)
    named = [(number, login.login_account(number, copy), copy) for number, copy in offered.items()]
    ranks = login._preferred()
    return sorted(named, key=lambda found: (ranks.get(found[1], len(ranks)), found[0]))


def _restored(number: int, copy: Mapping[str, str]) -> None:
    with login._locked(login.root(number)) as folder:
        if login.holders(folder):
            return
        login.home(number).mkdir(mode=login.PRIVATE_DIR, exist_ok=True)
        login.restore(copy, login.home(number))


def _home(environ: Mapping[str, str], number: int) -> Path:
    name = aicode_tokens.slug(environ.get("AGENTIHOOKS_AGENT_NAME") or f"pid-{os.getpid()}")
    with login._locked(Path(login.hooks_config.AGENTIHOOKS_HOME) / FOLDER / name) as folder:
        target = login._rebuild(folder, claude_home(environ), LINKED)
        account = login._read(login.home(number) / login.CONFIG).get(login.ACCOUNT)
        login._write(target / login.CONFIG, {**login._read(claude_json(environ)), login.ACCOUNT: account})
    if environ.get(binding.REPORT):
        binding.rehome(Path(environ[binding.REPORT]), target)
    return target


def prepare(environ: Mapping[str, str], cwd: Path) -> tuple[dict[str, str], str, int]:
    refused = login.refusal(claude_home(environ), cwd)
    if refused:
        raise login.LoginRefused(refused)
    candidates = _candidates(openbao_kv.from_env(environ))
    if not candidates:
        raise login.LoginRefused(EMPTY)
    now, reasons = time.time(), []
    for number, found, copy in candidates:
        left = master_logins.left(found, now)
        if quota_state.draining(left):
            reasons.append(SPENT.format(account=found, left=left))
            continue
        _restored(number, copy)
        child = {**login.child(environ, _home(environ, number), found), STORE: str(login.home(number))}
        try:
            native_login.check(child, found)
        except native_login.LoginError as exc:
            reasons.append(f"{found}: {exc}")
            continue
        return child, found, number
    raise login.LoginRefused(NONE.format(reasons="; ".join(reasons)))


def _front(environ: Mapping[str, str], cwd: Path) -> tuple[dict[str, str], str, int]:
    target, found = login.prepare(environ, cwd)
    child = login.child(environ, target, found)
    try:
        native_login.check(child, found)
    except native_login.LoginError as exc:
        login.release(target, environ)
        raise login.LoginRefused(str(exc)) from exc
    number = next(number for number in login.numbers() if login.home(number) == target)
    return child, found, number


def launch_claude(binary: str, args: list[str], environ: Mapping[str, str], report: str) -> None:
    from scripts.claude_quota_balancer import RoutingError
    from scripts.hive.account_slots import execute
    from scripts.install import _claude_command, _write_route_report

    try:
        title = next((args[index + 1] for index, flag in enumerate(args[:-1]) if flag == "--name"), "")
        front = front_door_route.check(environ.get("AGENTIHOOKS_PROFILE", ""), title, [])
        child, found, number = (_front if front else prepare)(environ, Path.cwd())
    except login.LoginRefused as exc:
        raise RoutingError(str(exc)) from exc
    _write_route_report(report, status="routed", account=found, placement="forced")
    print(f"[agenti] account={found} route={ROUTE} slot={number}", flush=True)
    execute(
        "claude",
        found,
        child,
        lambda env: os.execvpe(binary, _claude_command(binary, [*login.SETTINGS, *args]), env),
    )
