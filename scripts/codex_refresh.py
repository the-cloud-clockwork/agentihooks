"""`agentihooks codex refresh`: renew Codex accounts before their access token expires, on the refresh authority only.

Every grant rotates the refresh token, so exactly one machine may refresh a given account. No value is printed.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hooks.context.account_sessions import CODEX_TOKEN_PREFIX
from scripts import aicode_tokens, codex_login, codex_router, openbao_kv

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
STORE_ENV = "AGENTIHOOKS_AICODE_TOKEN_STORE"
DAYS_ENV = "AGENTIHOOKS_AICODE_TOKEN_REFRESH_DAYS"
DEFAULT_DAYS = 3.0
DAY_S = 86400
TIMEOUT_S = 20
TOKEN_KEYS = ("access_token", "refresh_token", "id_token")


class GrantError(RuntimeError):
    def __init__(self, status: int, error: str):
        super().__init__(f"HTTP {status} {error}".rstrip())
        self.status = status
        self.error = error


def grant(refresh_token: str, opener: Callable = urllib.request.urlopen) -> dict:
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    request = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read() or b"{}").get("error", "")
        except (ValueError, AttributeError):
            error = ""
        raise GrantError(exc.code, error if isinstance(error, str) else "") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GrantError(0, str(getattr(exc, "reason", exc))) from None


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


def _days_left(token: str, now: float) -> float:
    return (int(codex_router._claims(token).get("exp") or 0) - now) / DAY_S


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _renew(slug: str, access: str, refresh: str, run: Run) -> dict | None:
    now = run.deps.now()
    left = _days_left(access, now)
    if not run.force and left >= run.days:
        print(f"{slug}: {left:.1f}d left, ok")
        return None
    if not refresh:
        print(f"{slug}: refresh failed: no refresh token")
        return {}
    try:
        fresh = run.deps.grant(refresh)
    except GrantError as exc:
        print(f"{slug}: refresh failed {exc}")
        return {}
    if not fresh.get("access_token"):
        print(f"{slug}: refresh failed: the grant returned no access token")
        return {}
    fresh = {key: fresh[key] for key in TOKEN_KEYS if fresh.get(key)}
    fresh.setdefault("refresh_token", refresh)
    rotated = _digest(refresh) != _digest(fresh["refresh_token"])
    renewed = _days_left(fresh["access_token"], now)
    print(f"{slug}: {left:.1f}d left, refreshed, {renewed:.1f}d left, refresh token rotated={rotated}")
    return fresh


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _save_auth(path: Path, fresh: dict, now: float) -> None:
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        auth = {}
    auth = auth if isinstance(auth, dict) else {}
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    auth["tokens"] = {**tokens, **fresh}
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    _write_private(path, auth)


def _entries(slug: str, fresh: dict) -> dict[str, str]:
    return {
        f"{CODEX_TOKEN_PREFIX}{slug}": fresh["access_token"],
        f"{codex_login.REFRESH_PREFIX}{slug}": fresh["refresh_token"],
    }


def _store(bao: openbao_kv.OpenBao, entries: dict[str, str]) -> bool:
    try:
        total = openbao_kv.merge(bao, openbao_kv.CODEX_CREDS, entries)
    except openbao_kv.OpenBaoError as exc:
        print(f"OpenBao write failed: {exc}")
        return False
    print(openbao_kv.summary(openbao_kv.CODEX_CREDS, entries, total))
    return True


def _local_accounts(force: str) -> list[Path]:
    root = codex_login.homes()
    found = sorted(path for path in root.iterdir() if (path / "auth.json").is_file()) if root.is_dir() else []
    return [path for path in found if _valid(path.name) and (not force or path.name == force)]


def _valid(name: str) -> bool:
    try:
        return aicode_tokens.slug(name) == name
    except aicode_tokens.SlugError:
        return False


def run_local(run: Run) -> int:
    homes = _local_accounts(run.force)
    if run.force and not homes:
        print(f"{run.force}: no such Codex account under {codex_login.homes()}")
        return 1
    failures = 0
    for home in homes:
        tokens = codex_login._tokens(home)
        fresh = _renew(home.name, tokens.get("access_token", ""), tokens.get("refresh_token", ""), run)
        if fresh is None:
            continue
        if not fresh:
            failures += 1
            continue
        _save_auth(home / "auth.json", fresh, run.deps.now())
        aicode_tokens.write_rc(aicode_tokens.rc_file(), {f"{CODEX_TOKEN_PREFIX}{home.name}": fresh["access_token"]})
        if run.bao and not _store(run.bao, _entries(home.name, fresh)):
            failures += 1
    return failures


def _bao_accounts(secret: Mapping[str, str], force: str) -> list[str]:
    prefix = codex_login.REFRESH_PREFIX
    found = [key.removeprefix(prefix) for key in sorted(secret) if key.startswith(prefix)]
    return [slug for slug in found if _valid(slug) and (not force or slug == force)]


def _store_or_recover(run: Run, slug: str, fresh: dict) -> bool:
    if _store(run.bao, _entries(slug, fresh)):
        return True
    recovery = codex_login.homes() / slug / "auth.json"
    _save_auth(recovery, fresh, run.deps.now())
    print(f"{slug}: the new token pair is kept in {recovery}; store it in OpenBao before the next refresh")
    return False


def run_openbao(run: Run) -> int:
    try:
        secret, _ = openbao_kv.read(run.bao, openbao_kv.CODEX_CREDS)
    except openbao_kv.OpenBaoError as exc:
        print(f"OpenBao read failed: {exc}")
        return 1
    slugs = _bao_accounts(secret, run.force)
    if run.force and not slugs:
        print(f"{run.force}: no such Codex account in OpenBao")
        return 1
    failures = 0
    for slug in slugs:
        refresh = secret[f"{codex_login.REFRESH_PREFIX}{slug}"]
        fresh = _renew(slug, secret.get(f"{CODEX_TOKEN_PREFIX}{slug}", ""), refresh, run)
        if fresh is None:
            continue
        if not fresh:
            failures += 1
            continue
        failures += not _store_or_recover(run, slug, fresh)
        aicode_tokens.update_existing(aicode_tokens.rc_file(), {f"{CODEX_TOKEN_PREFIX}{slug}": fresh["access_token"]})
    return failures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks codex refresh")
    parser.add_argument("--force", default="", metavar="SLUG", help="refresh this account now")
    parser.add_argument("--days", type=float, default=None, help=f"refresh under N days left (default {DAYS_ENV})")
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
    path = Path.home() / ".agentihooks" / "codex-refresh.lock"
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
    store = (environ.get(STORE_ENV) or "local").strip().lower()
    if store not in ("local", "openbao"):
        raise ValueError(f"{STORE_ENV} must be local or openbao, not {store!r}")
    return store, Run(force, _days(options.days, environ), openbao_kv.from_env(environ, deps.opener), deps)


def main(args: list[str], environ: Mapping[str, str] | None = None, deps: Deps | None = None) -> int:
    environ = os.environ if environ is None else environ
    if environ.get(codex_login.REFRESH_FLAG, "false").strip().lower() != "true":
        print(f"{codex_login.REFRESH_FLAG} is not true: this machine is not the refresh authority")
        return 0
    try:
        store, run = _settings(args, environ, deps or Deps())
    except ValueError as exc:
        print(f"agentihooks codex refresh: {exc}")
        return 2
    if store == "openbao" and run.bao is None:
        print(f"{STORE_ENV}=openbao needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV}")
        return 1
    with _exclusive() as held:
        if not held:
            print("another codex refresh is already running on this machine; skipping")
            return 0
        failures = run_openbao(run) if store == "openbao" else run_local(run)
    return 1 if failures else 0
