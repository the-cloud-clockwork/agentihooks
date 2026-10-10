"""Route a Codex launch to one account: the default `codex login`, an AH_CX_TOKEN_<slug> or the api key.

Token values only move from the environment into the child's CODEX_ACCESS_TOKEN, an ordinary ChatGPT
token's account id claim into AGENTIHOOKS_CHATGPT_ACCOUNT_ID, and an api key stays in the child's
environment under its own name; none is printed or written anywhere.
"""

import base64
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from hooks.context.account_sessions import (
    API_ACCOUNT,
    CODEX_DEFAULT,
    CODEX_TOKEN_PREFIX,
    codex_sessions_by_account,
)
from scripts import codex_quota, session_bands
from scripts.codex_quota import CodexQuota
from scripts.routing import codex_api, envs, place
from scripts.routing.slots import API, INTERACTIVE, SUBSCRIPTION, Slot

TOKEN_ENV = envs.CODEX_TOKEN_ENV
PROBE_ARGS = ["exec", "--json", "--skip-git-repo-check", "Reply with the single word ok."]
PROBE_TIMEOUT_S = 90
CODEX_BIN = "codex"
ATTEMPTS_FILE = "codex-probe-attempts.json"
# A running app-server daemon answers account/read with its own auth, so a token session must not attach to it.
NO_DAEMON = "--no-daemon"
ACCOUNT_ID_ENV = envs.CHATGPT_ACCOUNT_ID_ENV
BEARER_PROVIDER = "agentihooks-chatgpt"
CHATGPT_BASE_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
AGENT_IDENTITY_CLAIM = "agent_runtime_id"
API_KEY_PREFIX = "sk-"
DEFAULT_KIND, API_KIND, CHATGPT_OAUTH, CODEX_ACCESS_TOKEN = "default", "api", "chatgpt-oauth", "codex-access-token"


@dataclass(frozen=True)
class CodexAccount:
    name: str
    env_name: str = ""
    signed_in: bool = True
    key_env: str = ""
    base_url: str = ""
    bearer: bool = False

    @property
    def is_token(self) -> bool:
        return bool(self.env_name)

    @property
    def is_api(self) -> bool:
        return bool(self.key_env)


class RoutingError(RuntimeError):
    pass


def _claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    with contextlib.suppress(ValueError):
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if isinstance(claims, dict):
            return claims
    return {}


def _chatgpt_account_id(claims: Mapping) -> str:
    auth = claims.get(OPENAI_AUTH_CLAIM)
    found = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return found if isinstance(found, str) else ""


def credential_kind(account: CodexAccount, environ: Mapping[str, str]) -> str:
    """Classifies only this account's credential, in memory; Codex itself reads every other token as its own."""
    if account.is_api:
        return API_KIND
    if not account.is_token:
        return DEFAULT_KIND
    token = environ.get(account.env_name, "")
    if not token:
        raise RoutingError(f"Codex account '{account.name}' has no token in {account.env_name}")
    if token.startswith(API_KEY_PREFIX):
        raise RoutingError(
            f"{account.env_name} holds an api key; put it in CODEX_API_KEY and route {API_ACCOUNT} instead"
        )
    claims = _claims(token)
    if AGENT_IDENTITY_CLAIM in claims or OPENAI_AUTH_CLAIM not in claims:
        return CODEX_ACCESS_TOKEN
    if not _chatgpt_account_id(claims):
        raise RoutingError(f"{account.env_name} holds a ChatGPT token with no chatgpt_account_id claim")
    return CHATGPT_OAUTH


def credential(account: CodexAccount, environ: Mapping[str, str]) -> CodexAccount:
    return replace(account, bearer=True) if credential_kind(account, environ) == CHATGPT_OAUTH else account


def bearer_overrides() -> list[str]:
    settings = {
        "name": "ChatGPT",
        "base_url": CHATGPT_BASE_URL,
        "env_key": TOKEN_ENV,
        "wire_api": "responses",
        "requires_openai_auth": False,
        "supports_websockets": False,
    }
    pairs = [f"model_provider={json.dumps(BEARER_PROVIDER)}"]
    pairs += [f"model_providers.{BEARER_PROVIDER}.{key}={json.dumps(value)}" for key, value in settings.items()]
    pairs.append(f"model_providers.{BEARER_PROVIDER}.env_http_headers.chatgpt-account-id={json.dumps(ACCOUNT_ID_ENV)}")
    return [part for pair in pairs for part in ("-c", pair)]


def token_accounts(environ: Mapping[str, str]) -> list[CodexAccount]:
    return [
        CodexAccount(name.removeprefix(CODEX_TOKEN_PREFIX), name)
        for name in sorted(environ)
        if name.startswith(CODEX_TOKEN_PREFIX)
        and name.removeprefix(CODEX_TOKEN_PREFIX) not in ("", API_ACCOUNT)
        and environ[name]
    ]


def _without_tokens(environ: Mapping[str, str]) -> dict[str, str]:
    return envs.codex_subscription_child(environ)


def default_signed_in(environ: Mapping[str, str], run: Callable = subprocess.run) -> bool:
    try:
        status = run(
            [shutil.which("codex") or "codex", "login", "status"],
            env=_without_tokens(environ),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return status.returncode == 0


def accounts(environ: Mapping[str, str], run: Callable = subprocess.run) -> list[CodexAccount]:
    return [CodexAccount(CODEX_DEFAULT, signed_in=default_signed_in(environ, run)), *token_accounts(environ)]


def routing_pool(environ: Mapping[str, str], run: Callable = subprocess.run) -> list[CodexAccount]:
    """Without token variables only the default login routes, unchecked, exactly as before tokens existed."""
    return accounts(environ, run) if token_accounts(environ) else [CodexAccount(CODEX_DEFAULT)]


def _registry() -> dict:
    from hooks.context.broadcast import _load_sessions

    return _load_sessions()


def quotas(pool: list[CodexAccount], environ: Mapping[str, str]) -> dict[str, CodexQuota | None]:
    """Each account's newest quota, from the session logs the session registry attributes to it."""
    owner = {session_id: info.get("account") for session_id, info in _registry().items()}
    tokens = {account.name for account in pool if account.is_token}

    def keep(account: CodexAccount) -> Callable[[str], bool]:
        if account.is_token:
            return lambda session_id: owner.get(session_id) == account.name
        return lambda session_id: owner.get(session_id) not in tokens

    return {account.name: codex_quota.latest_codex_quota(dict(environ), keep(account)) for account in pool}


def account_cap(quota: CodexQuota | None, now: float) -> int | None:
    """Live sessions under the session bands: the week alone sets it; None without a fresh reading."""
    if quota is None or not session_bands.fresh(quota.observed_at, now):
        return None
    return session_bands.week_cap(session_bands.left(quota.seven_day.used, quota.seven_day.resets_at, now))


def _thread(stdout: str) -> str:
    for line in stdout.splitlines():
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict) and event.get("type") == "thread.started":
                return str(event.get("thread_id") or "")
    return ""


def probe(account: CodexAccount, environ: Mapping[str, str], run: Callable = subprocess.run) -> CodexQuota | None:
    """A fresh reading from one tiny exec on the account, read from the rollout it writes."""
    codex_bin = shutil.which(CODEX_BIN) or CODEX_BIN
    try:
        account = credential(account, environ)
        done = run(
            command(account, codex_bin, PROBE_ARGS),
            env=child_environment(account, environ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired, RoutingError):
        return None
    thread = _thread(done.stdout) if done.stdout else ""
    return codex_quota.session_quota(dict(environ), thread) if thread else None


def _attempts_path() -> Path:
    return Path.home() / ".agentihooks" / ATTEMPTS_FILE


def _probe_attempts() -> dict[str, float]:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_attempts_path().read_text())
    return {}


def fresh_quotas(
    pool: list[CodexAccount], environ: Mapping[str, str], now: float, run: Callable = subprocess.run
) -> dict[str, CodexQuota | None]:
    """Each account's newest reading, probed again when it is missing or stale, at most once per freshness window."""
    found = quotas(pool, environ)
    attempts = _probe_attempts()
    for account in pool:
        seen = found.get(account.name)
        stale = not session_bands.fresh(seen.observed_at if seen else None, now)
        if account.signed_in and stale and not session_bands.fresh(attempts.get(account.name), now):
            attempts[account.name] = now
            found[account.name] = probe(account, environ, run) or seen
    with contextlib.suppress(OSError):
        _attempts_path().write_text(json.dumps(attempts))
    return found


@dataclass(frozen=True)
class CodexAccountSource:
    run: Callable | None = None
    sessions: Mapping[str, int] = field(default_factory=dict)
    refresh: bool = True

    def _run(self) -> tuple[Callable, ...]:
        return () if self.run is None else (self.run,)

    def pool(self, environ: Mapping[str, str]) -> list[CodexAccount]:
        return routing_pool(environ, *self._run())

    def readings(
        self, pool: list[CodexAccount], environ: Mapping[str, str], now: float
    ) -> dict[str, CodexQuota | None]:
        return fresh_quotas(pool, environ, now, *self._run()) if self.refresh else quotas(pool, environ)

    def offer(self, pool: list[CodexAccount], readings: Mapping[str, CodexQuota | None], now: float) -> list[Slot]:
        return [
            Slot(
                "codex",
                account.name,
                cap,
                self.sessions.get(account.name, 0),
                _spend_by(quota, now),
                kind=SUBSCRIPTION if account.is_token else INTERACTIVE,
            )
            for account in pool
            if account.signed_in and (cap := account_cap(quota := readings.get(account.name), now)) is not None
        ]

    def slots(self, environ: Mapping[str, str], now: float) -> list[Slot]:
        pool = self.pool(environ)
        return self.offer(pool, self.readings(pool, environ, now), now)

    def child_env(self, slot: Slot, environ: Mapping[str, str]) -> dict[str, str]:
        if slot.kind == INTERACTIVE:
            return child_environment(CodexAccount(CODEX_DEFAULT), environ)
        tokens = {account.name: account for account in token_accounts(environ)}
        if slot.account not in tokens:
            available = ", ".join(tokens) or "none"
            raise RoutingError(f"Codex account '{slot.account}' has no token; available: {available}")
        return child_environment(credential(tokens[slot.account], environ), environ)


def select(
    pool: list[CodexAccount],
    quotas: Mapping[str, CodexQuota | None],
    sessions: Mapping[str, int],
    now: float,
    route: str = "",
    environ: Mapping[str, str] | None = None,
) -> tuple[CodexAccount, str, session_bands.Seat | None]:
    """Split launches between the api and the account pool by weight, then take the free seat with the fewest sessions."""
    if route:
        for account in pool:
            if account.name == route:
                return account, "forced", None
        available = ", ".join(account.name for account in pool)
        raise RoutingError(f"Codex account '{route}' not found; available: {available}")
    by_name = {account.name: account for account in pool}
    api, weight = place.api_side(codex_api.CodexApiSource(sessions), "codex", environ or {}, now)
    pool_live = sum(sessions.get(account.name, 0) for account in pool)
    seat = place.place(api, CodexAccountSource(sessions=sessions).offer(pool, quotas, now), weight, pool_live)
    if seat is None:
        raise RoutingError("no signed in Codex account has a fresh reading and a free session under its quota band")
    if seat.kind == API:
        return api_account(environ or {}), "open", seat
    return by_name[seat.account], "open", seat


def seats(
    pool: list[CodexAccount], quotas: Mapping[str, CodexQuota | None], sessions: Mapping[str, int], now: float
) -> list[session_bands.Seat]:
    return CodexAccountSource(sessions=sessions).offer(pool, quotas, now)


def _spend_by(quota: CodexQuota, now: float) -> float | None:
    five = session_bands.left(quota.five_hour.used, quota.five_hour.resets_at, now)
    return session_bands.spend_by(five, session_bands.upcoming(quota.seven_day.resets_at, now))


def child_environment(account: CodexAccount, environ: Mapping[str, str]) -> dict[str, str]:
    if account.is_api:
        return envs.codex_api_child(environ)
    child = _without_tokens(environ)
    if account.is_token:
        child[account.env_name] = environ[account.env_name]
        child[TOKEN_ENV] = environ[account.env_name]
    if account.bearer:
        child[ACCOUNT_ID_ENV] = _chatgpt_account_id(_claims(environ[account.env_name]))
    return child


def command(account: CodexAccount, codex_bin: str, args: list[str]) -> list[str]:
    if account.is_api:
        return [codex_bin, NO_DAEMON, *codex_api.overrides(account.key_env, account.base_url), *args]
    if account.bearer:
        return [codex_bin, NO_DAEMON, *bearer_overrides(), *args]
    return [codex_bin, *([NO_DAEMON] if account.is_token else []), *args]


def api_account(environ: Mapping[str, str]) -> CodexAccount:
    key_env = codex_api.key_name(environ)
    if not key_env:
        raise RoutingError(f"Codex account '{API_ACCOUNT}' needs {' or '.join(codex_api.KEY_NAMES)}")
    base_url = codex_api.base_url(environ)
    if codex_api.carries_credentials(base_url):
        raise RoutingError(f"{codex_api.base_url_name(environ)} must not carry credentials, a query or a fragment")
    return CodexAccount(API_ACCOUNT, key_env=key_env, base_url=base_url)


def _take(args: list[str], flag: str) -> tuple[str, list[str]]:
    value, rest, index = "", [], 0
    while index < len(args):
        if args[index] == flag and index + 1 < len(args):
            value, index = args[index + 1], index + 2
            continue
        if args[index].startswith(f"{flag}="):
            value, index = args[index].split("=", 1)[1], index + 1
            continue
        rest.append(args[index])
        index += 1
    return value, rest


def _report(path: str, **fields: str) -> None:
    if path:
        from scripts.install import _write_route_report

        _write_route_report(path, **fields)


def _route(environ: Mapping[str, str], route: str, run: Callable) -> tuple[CodexAccount, str, int, str]:
    sessions = codex_sessions_by_account()
    if route == API_ACCOUNT:
        return api_account(environ), "forced", sessions.get(API_ACCOUNT, 0), "?"
    source = CodexAccountSource(run, refresh=not route)
    pool = source.pool(environ)
    now = time.time()
    found = source.readings(pool, environ, now)
    account, placement, seat = select(pool, found, sessions, now, route, environ)
    return account, placement, sessions.get(account.name, 0), str(seat.cap) if seat else "?"


def main(
    argv: list[str],
    environ: Mapping[str, str] | None = None,
    execvpe: Callable = os.execvpe,
    run: Callable = subprocess.run,
) -> int:
    active = dict(os.environ if environ is None else environ)
    report, args = _take(argv, "--agentihooks-report")
    route, args = _take(args, "--route")
    lock_path = Path(active.get("HOME", str(Path.home()))) / ".agentihooks" / "codex-route.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Held until exec: the descriptor is close-on-exec, so the next launch counts this one as a live session.
    lock = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        account, placement, live, cap = _route(active, route, run)
        account = credential(account, active)
    except RoutingError as exc:
        print(f"agentihooks codex: {exc}", file=sys.stderr)
        _report(report, status="failed", error=str(exc))
        return 3
    _report(report, status="routed", account=account.name, placement=placement)
    print(f"[agentihooks codex] account={account.name} sessions={live}/{cap} placement={placement}", flush=True)
    codex_bin = shutil.which("codex") or "codex"
    execvpe(codex_bin, command(account, codex_bin, args), child_environment(account, active))
    return 0
