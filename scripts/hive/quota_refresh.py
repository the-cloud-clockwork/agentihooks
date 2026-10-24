import shutil
import threading
import time
from collections.abc import Mapping

from scripts import claude_quota_balancer as balancer
from scripts import session_bands

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


def due(environ: Mapping[str, str], now: float) -> bool:
    return bool(stale(environ, now))


def refresh(environ: Mapping[str, str], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    credentials = stale(environ, now)
    if not credentials:
        return False
    balancer.collect_results(
        credentials,
        refresh=True,
        environ=environ,
        claude_bin=shutil.which("claude") or "claude",
        now=now,
    )
    return True


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
