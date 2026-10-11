"""Every grant rotates the refresh token, so exactly one machine may refresh a given account."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from hooks import config
from hooks.context.account_sessions import CODEX_TOKEN_PREFIX
from scripts import aicode_tokens, codex_login, codex_router, openbao_kv

PROG = "agentihooks codex refresh"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
STORE_ENV = "AGENTIHOOKS_AICODE_TOKEN_STORE"
DAYS_ENV = "AGENTIHOOKS_AICODE_TOKEN_REFRESH_DAYS"
LOCAL, OPENBAO = "local", "openbao"
DEFAULT_DAYS = 3.0
DAY_S = 86400
TIMEOUT_S = 20
TOKEN_KEYS = ("access_token", "refresh_token", "id_token")
RECOVERY_FILE = "recovery.json"
LOCK_FILE = "codex-refresh.lock"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NO_OAUTH_ERROR = "with no OAuth error"
NOT_AUTHORITY = f"{aicode_tokens.REFRESH_FLAG} is not true: this machine is not the refresh authority"
BAD_STORE = f"{STORE_ENV} must be {LOCAL} or {OPENBAO}, not {{store!r}}"
BAO_NEEDED = f"{STORE_ENV}={OPENBAO} needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV}"
BAO_UNSET = f"OpenBao not updated: {openbao_kv.URL_ENV} or {openbao_kv.TOKEN_ENV} unset"
BUSY = "another codex refresh is already running on this machine; skipping"
OK = "{slug}: {left:.1f}d left, ok"
REFRESHED = "{slug}: {left:.1f}d left, refreshed, {renewed:.1f}d left, refresh token rotated={rotated}"
FAILED = "{slug}: refresh failed: {reason}"
NO_REFRESH = "no refresh token"
NO_ACCESS = "the grant returned no access token"
WRITE_FAILED = "{slug}: write failed: {error}"
LOST = "{slug}: new token pair not persisted; run agentihooks codex login {slug}"
KEPT = "{slug}: the new token pair is kept in {path}; the next refresh stores it in OpenBao"
USING = "{slug}: using the newer pair kept in {path}"
STALE = "{slug}: removing {path}, older than the pair in OpenBao"
UNREADABLE = "{slug}: ignoring unreadable {path}"
NO_LOCAL_ACCOUNT = "{slug}: no such Codex account under {root}"
NO_BAO_ACCOUNT = "{slug}: no such Codex account in OpenBao"


class GrantError(RuntimeError):
    def __init__(self, status: int, error: str):
        super().__init__(f"HTTP {status} {error or NO_OAUTH_ERROR}" if status else f"unreachable: {error}")
        self.status = status
        self.error = error


def _oauth_error(exc: urllib.error.HTTPError) -> str:
    try:
        error = json.loads(exc.read()).get("error")
    except (ValueError, AttributeError):
        return ""
    return error if isinstance(error, str) else ""


def grant(refresh_token: str, opener: Callable = urllib.request.urlopen) -> dict:
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(TOKEN_URL, data=data, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise GrantError(exc.code, _oauth_error(exc)) from None
    except urllib.error.URLError as exc:
        raise GrantError(0, str(exc.reason)) from None
    except (OSError, ValueError) as exc:
        raise GrantError(0, str(exc)) from None
    return found if isinstance(found, dict) else {}


@dataclass(frozen=True)
class Deps:
    grant: Callable[[str], dict] = grant
    opener: Callable | None = None
    now: Callable[[], float] = field(default=time.time)


@dataclass(frozen=True)
class Run:
    force: str
    days: float
    bao: openbao_kv.OpenBao | None
    deps: Deps


class Outcome(NamedTuple):
    ok: bool
    saved: bool = False


GRANT_FAILED = Outcome(ok=False)


def _expiry(token: str | None) -> int | None:
    exp = codex_router._claims(str(token)).get("exp")
    return exp if isinstance(exp, int) else None


def _days_left(token: str | None, now: float) -> float:
    exp = _expiry(token)
    return 0.0 if exp is None else (exp - now) / DAY_S


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _failed(slug: str, reason: str) -> dict:
    print(FAILED.format(slug=slug, reason=reason))
    return {}


def _renew(slug: str, access: str | None, refresh: str | None, run: Run) -> dict | None:
    now = run.deps.now()
    left = _days_left(access, now)
    if not run.force and left >= run.days:
        print(OK.format(slug=slug, left=left))
        return None
    if not refresh:
        return _failed(slug, NO_REFRESH)
    try:
        fresh = run.deps.grant(refresh)
    except GrantError as exc:
        return _failed(slug, str(exc))
    if not fresh.get("access_token"):
        return _failed(slug, NO_ACCESS)
    fresh = {key: fresh[key] for key in TOKEN_KEYS if fresh.get(key)}
    fresh.setdefault("refresh_token", refresh)
    rotated = _digest(refresh) != _digest(fresh["refresh_token"])
    renewed = _days_left(fresh["access_token"], now)
    print(REFRESHED.format(slug=slug, left=left, renewed=renewed, rotated=rotated))
    return fresh


def _load(path: Path) -> dict:
    try:
        found = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _save_auth(path: Path, fresh: dict, now: float) -> None:
    auth = _load(path)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    auth["tokens"] = {**tokens, **fresh}
    auth["last_refresh"] = time.strftime(STAMP_FORMAT, time.gmtime(now))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    aicode_tokens.atomic_write(path, json.dumps(auth).encode(), 0o600)


def _entries(slug: str, fresh: dict) -> dict[str, str]:
    return {
        f"{CODEX_TOKEN_PREFIX}{slug}": fresh["access_token"],
        f"{codex_login.REFRESH_PREFIX}{slug}": fresh["refresh_token"],
    }


def _store(bao: openbao_kv.OpenBao, entries: dict[str, str]) -> bool:
    try:
        total = openbao_kv.merge(bao, openbao_kv.CODEX_CREDS, entries)
    except Exception as exc:
        print(f"OpenBao write failed: {exc}")
        return False
    print(openbao_kv.summary(openbao_kv.CODEX_CREDS, entries, total))
    return True


def _valid(name: str) -> bool:
    try:
        return aicode_tokens.slug(name) == name
    except aicode_tokens.SlugError:
        return False


def _local_accounts(force: str) -> list[Path]:
    root = codex_login.homes()
    found = sorted(path for path in root.iterdir() if (path / "auth.json").is_file()) if root.is_dir() else []
    return [path for path in found if _valid(path.name) and (not force or path.name == force)]


def _attempt(slug: str, action: Callable[[], object]) -> bool:
    try:
        action()
    except OSError as exc:
        print(WRITE_FAILED.format(slug=slug, error=exc))
        return False
    return True


def _refresh_local(run: Run, home: Path) -> Outcome | None:
    slug = home.name
    tokens = codex_login._tokens(home)
    fresh = _renew(slug, tokens.get("access_token"), tokens.get("refresh_token"), run)
    if fresh is None:
        return None
    if not fresh:
        return GRANT_FAILED
    saved = _attempt(slug, lambda: _save_auth(home / "auth.json", fresh, run.deps.now()))
    rc_entry = {f"{CODEX_TOKEN_PREFIX}{slug}": fresh["access_token"]}
    block = _attempt(slug, lambda: aicode_tokens.write_rc(aicode_tokens.rc_file(), rc_entry))
    stored = _store(run.bao, _entries(slug, fresh)) if run.bao else None
    if not saved and not stored:
        print(LOST.format(slug=slug))
    return Outcome(saved and block and stored is not False, saved)


def run_local(run: Run) -> int:
    homes = _local_accounts(run.force)
    if run.force and not homes:
        print(NO_LOCAL_ACCOUNT.format(slug=run.force, root=codex_login.homes()))
        return 1
    outcomes = [outcome for home in homes if (outcome := _refresh_local(run, home)) is not None]
    if run.bao is None and any(outcome.saved for outcome in outcomes):
        print(BAO_UNSET)
    return sum(not outcome.ok for outcome in outcomes)


def _bao_accounts(secret: Mapping[str, str], force: str) -> list[str]:
    prefix = codex_login.REFRESH_PREFIX
    found = [key.removeprefix(prefix) for key in sorted(secret) if key.startswith(prefix)]
    return [slug for slug in found if _valid(slug) and (not force or slug == force)]


def _recovery(slug: str) -> Path:
    return codex_login.homes() / slug / RECOVERY_FILE


def _recovered(slug: str, access: str | None) -> dict | None:
    path = _recovery(slug)
    if not path.exists():
        return None
    tokens = codex_login._tokens(path.parent, path.name)
    kept = _expiry(tokens.get("access_token"))
    if not tokens.get("refresh_token") or kept is None:
        print(UNREADABLE.format(slug=slug, path=path))
        return None
    current = _expiry(access)
    if current is not None and kept <= current:
        print(STALE.format(slug=slug, path=path))
        _attempt(slug, path.unlink)
        return None
    return {key: tokens[key] for key in TOKEN_KEYS if tokens.get(key)}


def _store_or_recover(run: Run, slug: str, fresh: dict) -> bool:
    if _store(run.bao, _entries(slug, fresh)):
        return True
    recovery = _recovery(slug)
    if _attempt(slug, lambda: _save_auth(recovery, fresh, run.deps.now())):
        print(KEPT.format(slug=slug, path=recovery))
    else:
        print(LOST.format(slug=slug))
    return False


def _refresh_bao(run: Run, secret: Mapping[str, str], slug: str) -> bool:
    access, refresh = secret.get(f"{CODEX_TOKEN_PREFIX}{slug}"), secret[f"{codex_login.REFRESH_PREFIX}{slug}"]
    recovered = _recovered(slug, access)
    if recovered:
        print(USING.format(slug=slug, path=_recovery(slug)))
        access, refresh = recovered["access_token"], recovered["refresh_token"]
    fresh = _renew(slug, access, refresh, run)
    if fresh == {}:
        return False
    fresh = fresh or recovered
    if not fresh:
        return True
    stored = _store_or_recover(run, slug, fresh)
    if stored and recovered:
        _attempt(slug, _recovery(slug).unlink)
    rc_entry = {f"{CODEX_TOKEN_PREFIX}{slug}": fresh["access_token"]}
    block = _attempt(slug, lambda: aicode_tokens.update_existing(aicode_tokens.rc_file(), rc_entry))
    return stored and block


def run_openbao(run: Run) -> int:
    try:
        secret, _ = openbao_kv.read(run.bao, openbao_kv.CODEX_CREDS)
    except openbao_kv.OpenBaoError as exc:
        print(f"OpenBao read failed: {exc}")
        return 1
    slugs = _bao_accounts(secret, run.force)
    if run.force and not slugs:
        print(NO_BAO_ACCOUNT.format(slug=run.force))
        return 1
    return [_refresh_bao(run, secret, slug) for slug in slugs].count(False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("--force")
    parser.add_argument("--days", type=float)
    return parser


def _days(option: float | None, environ: Mapping[str, str]) -> float:
    if option is not None:
        return option
    try:
        return float(environ.get(DAYS_ENV) or DEFAULT_DAYS)
    except ValueError:
        return DEFAULT_DAYS


@contextlib.contextmanager
def _exclusive() -> Iterator[bool]:
    path = Path(config.AGENTIHOOKS_HOME) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _settings(args: list[str], environ: Mapping[str, str], deps: Deps) -> tuple[str, Run]:
    options = _parser().parse_args(args)
    force = aicode_tokens.slug(options.force) if options.force else ""
    store = (environ.get(STORE_ENV) or LOCAL).strip().lower()
    if store not in (LOCAL, OPENBAO):
        raise ValueError(BAD_STORE.format(store=store))
    return store, Run(force, _days(options.days, environ), openbao_kv.from_env(environ, deps.opener), deps)


def main(args: list[str], environ: Mapping[str, str] | None = None, deps: Deps | None = None) -> int:
    environ = os.environ if environ is None else environ
    if not aicode_tokens.refresh_authority(environ):
        print(NOT_AUTHORITY)
        return 0
    try:
        store, run = _settings(args, environ, deps or Deps())
    except ValueError as exc:
        print(f"{PROG}: {exc}")
        return 2
    if store == OPENBAO and run.bao is None:
        print(BAO_NEEDED)
        return 1
    with _exclusive() as held:
        if not held:
            print(BUSY)
            return 0
        failures = run_openbao(run) if store == OPENBAO else run_local(run)
    return 1 if failures else 0
