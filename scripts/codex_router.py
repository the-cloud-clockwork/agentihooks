"""Route a Codex launch to one account: the default `codex login`, an AH_CX_TOKEN_<slug> or the api key.

Token values only move from the environment into the child's CODEX_ACCESS_TOKEN, an ordinary ChatGPT
token's account id claim into AGENTIHOOKS_CHATGPT_ACCOUNT_ID, and an api key stays in the child's
environment under its own name; none is printed or written anywhere.
"""

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.context.account_sessions import (
    API_ACCOUNT,
    CODEX_DEFAULT,
    CODEX_TOKEN_PREFIX,
    INTERNAL_RUN,
    codex_sessions_by_account,
)
from scripts import (
    account_names,
    codex_quota,
    codex_rollouts,
    codex_sessions,
    codex_usage,
    operator_env,
    quota_freshness,
    session_bands,
)
from scripts.codex_quota import CodexQuota
from scripts.routing import codex_api, envs, place
from scripts.routing.slots import API, INTERACTIVE, SUBSCRIPTION, Slot

if TYPE_CHECKING:
    from scripts.routing.reservation import Reservation

TOKEN_ENV = envs.CODEX_TOKEN_ENV
PROBE_ARGS = ["exec", "--json", "--skip-git-repo-check", "Reply with the single word ok."]
PROBE_TIMEOUT_S = 90
CODEX_BIN = "codex"
ATTEMPTS_FILE = "codex-probe-attempts.json"
READING_MAX_AGE_ENV = "AGENTIHOOKS_CODEX_READING_MAX_AGE_H"
READING_MAX_AGE_H = 6.0
HOUR_S = 3600
# A running app-server daemon answers account/read with its own auth, so a token session must not attach to it.
NO_DAEMON = "--no-daemon"
ACCOUNT_ID_ENV = envs.CHATGPT_ACCOUNT_ID_ENV
BEARER_PROVIDER = "agentihooks-chatgpt"
CHATGPT_BASE_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"
EMAIL_SLUG = re.compile(r"[a-z0-9_]+at[a-z0-9_]+_[a-z]{2,}")
AGENT_IDENTITY_CLAIM = "agent_runtime_id"
API_KEY_PREFIX = "sk-"
PERSONAL_TOKEN_PREFIX = "at-"
DEFAULT_KIND = "default"
API_KIND = "api"
OAUTH_KIND = "chatgpt-oauth"
ACCESS_TOKEN_KIND = "codex-access-token"
LOGIN_PASSTHROUGH = ("status", "help")
FINGERPRINT_LENGTH = 8


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
    def kind(self) -> str:
        return SUBSCRIPTION if self.is_token else INTERACTIVE

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
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
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
    token = environ.get(account.env_name)
    if not token:
        raise RoutingError(f"Codex account '{account.name}' has no token in {account.env_name}")
    if token.startswith(PERSONAL_TOKEN_PREFIX):
        return ACCESS_TOKEN_KIND
    if token.startswith(API_KEY_PREFIX):
        raise RoutingError(
            f"{account.env_name} holds an api key; put it in CODEX_API_KEY and route {API_ACCOUNT} instead"
        )
    claims = _claims(token)
    if AGENT_IDENTITY_CLAIM in claims or OPENAI_AUTH_CLAIM not in claims:
        return ACCESS_TOKEN_KIND
    if not _chatgpt_account_id(claims):
        raise RoutingError(f"{account.env_name} holds a ChatGPT token with no chatgpt_account_id claim")
    return OAUTH_KIND


def credential(account: CodexAccount, environ: Mapping[str, str]) -> CodexAccount:
    return replace(account, bearer=True) if credential_kind(account, environ) == OAUTH_KIND else account


def refusal(account: CodexAccount, environ: Mapping[str, str]) -> str:
    try:
        credential_kind(account, environ)
    except RoutingError as exc:
        return str(exc)
    return ""


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
    names = [
        name
        for name in environ
        if name.startswith(CODEX_TOKEN_PREFIX)
        and name.removeprefix(CODEX_TOKEN_PREFIX) not in ("", API_ACCOUNT)
        and environ[name]
    ]
    return [CodexAccount(name.removeprefix(CODEX_TOKEN_PREFIX), name) for name in operator_env.distinct(environ, names)]


def _without_tokens(environ: Mapping[str, str]) -> dict[str, str]:
    return envs.codex_subscription_child(environ)


def default_signed_in(environ: Mapping[str, str], run: Callable = subprocess.run) -> bool:
    try:
        status = run(
            [shutil.which("codex") or "codex", "login", "status"],
            env={**_without_tokens(environ), INTERNAL_RUN: "1"},
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return status.returncode == 0


def accounts(environ: Mapping[str, str], run: Callable = subprocess.run) -> list[CodexAccount]:
    return token_accounts(environ) or [CodexAccount(CODEX_DEFAULT, signed_in=default_signed_in(environ, run))]


def routing_pool(environ: Mapping[str, str], run: Callable = subprocess.run) -> list[CodexAccount]:
    return token_accounts(environ) or [CodexAccount(CODEX_DEFAULT)]


def live_accounts(pool: list[CodexAccount], sessions: Mapping[str, int]) -> list[CodexAccount]:
    """Token accounts live sessions run on that the pool lacks, since a routed session holds only its own token."""
    known = {account.name for account in pool}
    return [
        CodexAccount(name, f"{CODEX_TOKEN_PREFIX}{name}")
        for name in sessions
        if name not in known and name not in {CODEX_DEFAULT, API_ACCOUNT}
    ]


def _registry() -> dict:
    from hooks.context.broadcast import _load_sessions

    return _load_sessions()


def quotas(pool: list[CodexAccount], environ: Mapping[str, str]) -> dict[str, CodexQuota | None]:
    """Each account's newest quota from its own sessions folder, else the session logs the registry attributes to it."""
    owner = {session_id: info.get("account") for session_id, info in _registry().items()}
    tokens = {account.name for account in [*pool, *token_accounts(environ)] if account.is_token}
    linked = set().union(*(codex_rollouts.session_ids(environ, name) for name in tokens))

    def keep(account: CodexAccount) -> Callable[[str], bool]:
        if account.is_token:
            return lambda session_id: owner.get(session_id) == account.name
        if not tokens:
            return lambda session_id: True
        return lambda session_id: session_id in owner and owner[session_id] not in tokens and session_id not in linked

    def read(account: CodexAccount) -> CodexQuota | None:
        own = codex_rollouts.folder(environ, account.name)
        found = codex_quota.latest_codex_quota(sessions=own) if own.is_dir() else None
        return found or codex_quota.latest_codex_quota(dict(environ), keep(account))

    usage = codex_usage.cached()
    return {account.name: _newest(read(account), usage.get(account.name)) for account in pool}


def _newest(found: CodexQuota | None, cached: CodexQuota | None) -> CodexQuota | None:
    if found is None or cached is None:
        return found or cached
    return max(found, cached, key=lambda reading: reading.observed_at)


def _reading_max_age(environ: Mapping[str, str]) -> float:
    try:
        return float(environ.get(READING_MAX_AGE_ENV) or READING_MAX_AGE_H) * HOUR_S
    except ValueError:
        return READING_MAX_AGE_H * HOUR_S


def usable(quota: CodexQuota, now: float, environ: Mapping[str, str] | None = None) -> bool:
    if session_bands.fresh(quota.observed_at, now):
        return True
    return now - quota.observed_at <= _reading_max_age(os.environ if environ is None else environ)


def stale_reset(quota: CodexQuota, now: float, environ: Mapping[str, str] | None = None) -> bool:
    """A reading past its max age whose week has reset since says nothing about the new week."""
    return not usable(quota, now, environ) and codex_quota.passed(quota.seven_day, now)


def account_cap(quota: CodexQuota | None, now: float, environ: Mapping[str, str] | None = None) -> int | None:
    """Live sessions under the session bands: the week alone sets it; None without a usable reading."""
    if quota is None:
        return None
    if quota.expired:
        return 0
    if not usable(quota, now, environ):
        return None
    if quota.reached and not codex_quota.passed(quota.seven_day, now):
        return 0
    return session_bands.week_cap(session_bands.left(quota.seven_day.used, quota.seven_day.resets_at, now))


def _thread(stdout: str) -> str:
    for line in stdout.splitlines():
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict) and event.get("type") == "thread.started":
                return str(event.get("thread_id") or "")
    return ""


def _default_token(environ: Mapping[str, str]) -> str | None:
    try:
        tokens = json.loads((codex_quota.codex_home(dict(environ)) / "auth.json").read_text()).get("tokens")
    except (OSError, ValueError, AttributeError):
        return None
    found = tokens.get("access_token") if isinstance(tokens, dict) else None
    return found if isinstance(found, str) else None


def _bearer(account: CodexAccount, environ: Mapping[str, str]) -> tuple[str, str] | None:
    if account.is_token:
        token = environ[account.env_name] if credential_kind(account, environ) == OAUTH_KIND else None
    else:
        token = None if account.is_api else _default_token(environ)
    claim = _chatgpt_account_id(_claims(token)) if token else ""
    return (token, claim) if claim else None


def identity(account: CodexAccount, environ: Mapping[str, str]) -> str:
    """The ChatGPT account id claim behind this account's credential; empty when its credential carries none."""
    if account.is_api:
        return ""
    return _token_identity(environ.get(account.env_name) if account.is_token else _default_token(environ))


def _token_identity(token: str | None) -> str:
    return _chatgpt_account_id(_claims(token)) if token else ""


def fingerprint(account_id: str) -> str:
    return hashlib.sha256(account_id.encode()).hexdigest()[:FINGERPRINT_LENGTH] if account_id else ""


def duplicates(pool: list[CodexAccount], environ: Mapping[str, str]) -> dict[str, str]:
    """Each token account whose ChatGPT account another token account shares, mapped to the one routed for both."""
    groups: dict[str, list[CodexAccount]] = {}
    for account in pool:
        found = identity(account, environ) if account.is_token else ""
        if found:
            groups.setdefault(found, []).append(account)
    repeated = {}
    for group in groups.values():
        kept = min(group, key=lambda account: _routed_rank(account, environ))
        repeated.update({account.name: kept.name for account in group if account is not kept})
    return repeated


def _routed_rank(account: CodexAccount, environ: Mapping[str, str]) -> tuple[bool, bool, str]:
    claims = _claims(environ[account.env_name])
    profile = claims.get(OPENAI_PROFILE_CLAIM)
    email = profile.get("email") if isinstance(profile, dict) else None
    named = isinstance(email, str) and account.name == account_names.secret_key(email).replace("-", "_")
    return not named, not EMAIL_SLUG.fullmatch(account.name), account.name


def routed(pool: list[CodexAccount], environ: Mapping[str, str]) -> list[CodexAccount]:
    repeated = duplicates(pool, environ)
    return [account for account in pool if account.name not in repeated]


def routed_live(
    pool: list[CodexAccount], sessions: Mapping[str, int], environ: Mapping[str, str]
) -> list[CodexAccount]:
    repeated = duplicates(token_accounts(environ), environ)
    return [account for account in live_accounts(pool, sessions) if account.name not in repeated]


def identity_warnings(environ: Mapping[str, str]) -> list[str]:
    return [
        f"{CODEX_TOKEN_PREFIX}{name} is the same ChatGPT account as {earlier}; it is not routed"
        for name, earlier in duplicates(token_accounts(environ), environ).items()
    ]


def default_distinction(environ: Mapping[str, str]) -> str:
    """One line naming whether the default login is a ChatGPT account no token account repeats."""
    found = {account.name: identity(account, environ) for account in token_accounts(environ)}
    if not found:
        return ""
    default = _token_identity(_default_token(environ))
    if not default:
        return "codex default login: identity unknown"
    same = [name for name, account_id in found.items() if account_id == default]
    verdict = f"same ChatGPT account as {', '.join(same)}" if same else f"distinct from {', '.join(found)}"
    return f"codex default login #{fingerprint(default)}: {verdict}"


def probe(account: CodexAccount, environ: Mapping[str, str], run: Callable = subprocess.run) -> CodexQuota | None:
    """A fresh reading: the free usage read for a ChatGPT token, else one tiny exec read from its rollout."""
    codex_bin = shutil.which(CODEX_BIN) or CODEX_BIN
    try:
        bearer = _bearer(account, environ)
        if bearer:
            found = codex_usage.read(*bearer, time.time())
            if found:
                codex_usage.store(account.name, found)
                return found
        account = credential(account, environ)
        done = run(
            command(account, codex_bin, PROBE_ARGS),
            env={**child_environment(account, environ), INTERNAL_RUN: "1"},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired, RoutingError):
        return None
    thread = _thread(done.stdout) if done.stdout else ""
    if not thread:
        return None
    codex_rollouts.link_thread(environ, account.name, thread)
    return codex_quota.session_quota(dict(environ), thread)


def _attempts_path() -> Path:
    return Path.home() / ".agentihooks" / ATTEMPTS_FILE


def _probe_attempts() -> dict[str, float]:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_attempts_path().read_text())
    return {}


def fresh_quotas(
    pool: list[CodexAccount],
    environ: Mapping[str, str],
    now: float,
    run: Callable = subprocess.run,
    force: bool = False,
) -> dict[str, CodexQuota | None]:
    """Each account's newest reading, probed again when it is missing or stale, at most once per freshness window;
    ``force`` probes every signed in account."""
    found = quotas(pool, environ)
    attempts = _probe_attempts()
    for account in pool:
        seen = found.get(account.name)
        stale = seen is None or quota_freshness.stale(
            seen.observed_at, (seen.five_hour.resets_at, seen.seven_day.resets_at), now
        )
        due = force or (stale and not session_bands.fresh(attempts.get(account.name), now))
        if account.signed_in and due:
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
    held: "Reservation | None" = None

    def _run(self) -> tuple[Callable, ...]:
        return () if self.run is None else (self.run,)

    def pool(self, environ: Mapping[str, str]) -> list[CodexAccount]:
        found = [
            account
            for account in accounts(environ, *self._run())
            if account.signed_in and not refusal(account, environ)
        ]
        return routed(found, environ)

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
                kind=account.kind,
            )
            for account in pool
            if account.signed_in
            and (cap := self._cap(account.name, quota := readings.get(account.name), now)) is not None
        ]

    def _cap(self, name: str, quota: "CodexQuota | None", now: float) -> int | None:
        if self.held is not None and quota is not None and self.held.closes(name, quota.seven_day.used):
            return 0
        return account_cap(quota, now)

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
                if not account.signed_in:
                    raise RoutingError(f"Codex account '{route}' is not signed in; run codex login")
                return account, "forced", None
        available = ", ".join(account.name for account in pool)
        raise RoutingError(f"Codex account '{route}' not found; available: {available}")
    by_name = {account.name: account for account in pool}
    api, weight = place.api_side(codex_api.CodexApiSource(sessions), "codex", environ or {}, now)
    pool_live = sum(sessions.get(account.name, 0) for account in pool)
    from scripts.routing import reservation

    held = reservation.for_worker("codex", environ or {})
    seat = place.place(
        api, CodexAccountSource(sessions=sessions, held=held).offer(pool, quotas, now), weight, pool_live
    )
    if seat is None:
        refused = [text for account in token_accounts(environ or {}) if (text := refusal(account, environ or {}))]
        no_seat = "no signed in Codex account has a fresh reading and a free session under its quota band"
        raise RoutingError("; ".join([no_seat, *refused]))
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
    environ = {name: value for name, value in environ.items() if name != INTERNAL_RUN}
    if account.is_api:
        return envs.codex_api_child(environ)
    child = _without_tokens(environ)
    if account.is_token:
        child[account.env_name] = environ[account.env_name]
        child[TOKEN_ENV] = environ[account.env_name]
    if account.bearer:
        child[ACCOUNT_ID_ENV] = _chatgpt_account_id(_claims(environ[account.env_name]))
    return child


def _exec_at(args: list[str]) -> int | None:
    value_flags = codex_sessions.VALUE_FLAGS | {"--local-provider", "--remote", "--remote-auth-token-env", "--add-dir"}
    mode = "options"
    for at, arg in enumerate(args):
        if mode == "value":
            mode = "options"
            continue
        if mode == "images":
            if not arg.startswith("-"):
                continue
            mode = "options"
        if mode == "options":
            if arg == "--":
                return None
            if arg == "--image" or arg.startswith(("-i", "--image=")):
                mode = "images"
            elif arg in value_flags:
                mode = "value"
            elif not arg.startswith("-"):
                return at if arg in ("exec", "e") else None
    return None


def command(account: CodexAccount, codex_bin: str, args: list[str]) -> list[str]:
    if account.is_api:
        overrides = codex_api.overrides(account.key_env, account.base_url)
    elif account.bearer:
        overrides = bearer_overrides()
    else:
        return [codex_bin, *([NO_DAEMON] if account.is_token else []), *args]
    at = _exec_at(args)
    if at is not None:
        return [codex_bin, NO_DAEMON, *args[: at + 1], *overrides, *args[at + 1 :]]
    return [codex_bin, NO_DAEMON, *overrides, *args]


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


def _route(environ: Mapping[str, str], route: str, run: Callable, exclude: str) -> tuple[CodexAccount, str, int, str]:
    sessions = codex_sessions_by_account()
    if route == API_ACCOUNT:
        return api_account(environ), "forced", sessions.get(API_ACCOUNT, 0), "?"
    source = CodexAccountSource(run, refresh=not route)
    pool = accounts(environ, run) if route else source.pool(environ)
    pool = [account for account in pool if route or account.name != exclude]
    now = time.time()
    found = source.readings(pool, environ, now)
    account, placement, seat = select(pool, found, sessions, now, route, environ)
    return account, placement, sessions.get(account.name, 0), str(seat.cap) if seat else "?"


def _own_command(args: list[str], environ: Mapping[str, str]) -> int | None:
    if args[:1] == ["refresh"] and (len(args) == 1 or args[1].startswith("-")):
        from scripts import codex_refresh

        return codex_refresh.main(args[1:], environ)
    if args[:1] == ["login"] and len(args) > 1 and not _passthrough_login(args[1]):
        from scripts import codex_login

        return codex_login.main(args[1:], environ)
    if args[:1] == ["logout"] and len(args) > 1 and not _passthrough_login(args[1]):
        from scripts import codex_logout

        return codex_logout.main(args[1:], environ)
    return None


def _passthrough_login(word: str) -> bool:
    return word.lower() in LOGIN_PASSTHROUGH or word.startswith("-")


def main(
    argv: list[str],
    environ: Mapping[str, str] | None = None,
    execvpe: Callable = os.execvpe,
    run: Callable = subprocess.run,
) -> int:
    active = dict(os.environ if environ is None else environ)
    own = _own_command(argv, active)
    if own is not None:
        return own
    report, args = _take(argv, "--agentihooks-report")
    route, args = _take(args, "--route")
    exclude, args = _take(args, "--agentihooks-exclude")
    args = codex_sessions.resolve(args, _without_tokens(active))
    for warning in identity_warnings(active):
        print(f"agentihooks codex: warning: {warning}", file=sys.stderr)
    lock_path = Path(active.get("HOME", str(Path.home()))) / ".agentihooks" / "codex-route.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Held until exec: the descriptor is close-on-exec, so the next launch counts this one as a live session.
    lock = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        if route == "interactive":
            from scripts.routing import interactive

            declared = interactive.account("codex", active, run)
            account, placement, live, cap = CodexAccount(declared.slug), "forced", 0, "?"
        else:
            account, placement, live, cap = _route(active, route, run, exclude)
            account = credential(account, active)
    except RoutingError as exc:
        print(f"agentihooks codex: {exc}", file=sys.stderr)
        _report(report, status="failed", error=str(exc))
        lock.close()
        return 3
    _report(report, status="routed", account=account.name, placement=placement)
    print(f"[agentihooks codex] account={account.name} sessions={live}/{cap} placement={placement}", flush=True)
    codex_bin = shutil.which("codex") or "codex"
    from scripts.deps_preflight import refresh_codex_wrapper

    refresh_codex_wrapper("codex")
    child = (
        envs.codex_interactive_child(active, account.name)
        if route == "interactive"
        else child_environment(account, active)
    )
    from scripts.hive.account_slots import execute

    execute("codex", account.name, child, lambda env: execvpe(codex_bin, command(account, codex_bin, args), env))
    lock.close()
    return 0
