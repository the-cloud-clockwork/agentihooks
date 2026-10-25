import shutil
import threading
import time
from collections.abc import Mapping

from scripts import claude_quota_balancer as balancer
from scripts import codex_router, quota_freshness, session_bands

MARGIN_SECONDS = 5 * 60
RETRY_SECONDS = MARGIN_SECONDS


def stale(environ: Mapping[str, str], now: float) -> list[balancer.Credential]:
    credentials = balancer.discover_credentials(environ)
    if not credentials:
        return []
    newest: dict[str, float] = {}
    for at, result in balancer.cached_observations(environ=environ):
        newest[result.account] = max(at, newest.get(result.account, at))
    deadline = now - (session_bands.FRESH_SECONDS - MARGIN_SECONDS)
    return [c for c in credentials if c.account not in newest or newest[c.account] < deadline]


def stale_codex(environ: Mapping[str, str], now: float) -> list[codex_router.CodexAccount]:
    pool = codex_router.routed([account for account in codex_router.accounts(environ) if account.signed_in], environ)
    readings = codex_router.quotas(pool, environ)
    return [
        account
        for account in pool
        if (reading := readings.get(account.name)) is None
        or quota_freshness.stale(reading.observed_at, (reading.five_hour.resets_at, reading.seven_day.resets_at), now)
    ]


def due(environ: Mapping[str, str], now: float) -> bool:
    return bool(stale(environ, now) or stale_codex(environ, now))


def refresh(environ: Mapping[str, str], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    credentials = stale(environ, now)
    codex = stale_codex(environ, now)
    if credentials:
        balancer.collect_results(
            credentials,
            refresh=True,
            environ=environ,
            claude_bin=shutil.which("claude") or "claude",
            now=now,
        )
    if codex:
        codex_router.fresh_quotas(codex, environ, now)
    return bool(credentials or codex)


def start(
    environ: Mapping[str, str], worker: threading.Thread | None, attempted_at: float, now: float | None = None
) -> tuple[threading.Thread | None, float]:
    now = time.time() if now is None else now
    if worker is not None and worker.is_alive():
        return worker, attempted_at
    if now - attempted_at < RETRY_SECONDS or not due(environ, now):
        return worker, attempted_at
    worker = threading.Thread(target=refresh, args=(environ, now), daemon=True)
    worker.start()
    return worker, now
