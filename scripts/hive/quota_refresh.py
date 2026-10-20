import shutil
import threading
import time
from collections.abc import Mapping

from scripts import claude_quota_balancer as balancer
from scripts import session_bands

MARGIN_SECONDS = 5 * 60


def due(environ: Mapping[str, str], now: float) -> bool:
    accounts = {credential.account for credential in balancer.discover_credentials(environ)}
    if not accounts:
        return False
    newest: dict[str, float] = {}
    for at, result in balancer.cached_observations(environ=environ):
        newest[result.account] = max(at, newest.get(result.account, at))
    deadline = now - (session_bands.FRESH_SECONDS - MARGIN_SECONDS)
    return any(account not in newest or newest[account] < deadline for account in accounts)


def refresh(environ: Mapping[str, str], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if not due(environ, now):
        return False
    balancer.collect_results(
        balancer.discover_credentials(environ),
        refresh=True,
        environ=environ,
        claude_bin=shutil.which("claude") or "claude",
        now=now,
    )
    return True


def start(
    environ: Mapping[str, str], worker: threading.Thread | None, now: float | None = None
) -> threading.Thread | None:
    if worker is not None and worker.is_alive():
        return worker
    now = time.time() if now is None else now
    if not due(environ, now):
        return worker
    worker = threading.Thread(target=refresh, args=(environ, now), daemon=True)
    worker.start()
    return worker
