"""The heartbeat each completed tick pass writes, judged by the liveness probe and the stale watcher, never the tick."""

import json
import time
from dataclasses import asdict, dataclass

from scripts.swarm import capacity

KEY = "tick-heartbeat"
LIMIT_MS = 600_000
HOST, QUOTA, LEDGER = "host room", "quota", "ledger revision"
JUDGED = ("running", "paused", "stopping", "drained")
HALTS = ("ledger-gone", "ledger-binned")


@dataclass(frozen=True)
class Heartbeat:
    at: int
    observed: dict
    rev: int | None = None


def read(store, slug) -> Heartbeat | None:
    raw = store.redis.get(store.key(slug, KEY))
    return Heartbeat(**json.loads(raw)) if raw else None


def write(store, slug, at: int, doc: dict) -> Heartbeat:
    """The ledger probe writes the ledger every pass, so a revision that holds still is a write that never lands."""
    decision, previous = capacity.read(store, slug), read(store, slug)
    rev = (doc.get("_meta") or {}).get("rev")
    seen = [account["observed_at"] for account in decision.get("accounts", []) if account.get("observed_at")]
    kept = previous is not None and rev is not None and previous.rev == rev and previous.observed.get(LEDGER)
    beat = Heartbeat(
        at,
        {
            HOST: decision.get("observed_at"),
            QUOTA: round(max(seen) * 1000) if seen else None,
            LEDGER: previous.observed[LEDGER] if kept else (at if rev is not None else None),
        },
        rev,
    )
    store.redis.set(store.key(slug, KEY), json.dumps(asdict(beat)))
    return beat


def halted(store, slug) -> bool:
    """A restart cannot end a ledger that is gone, binned or in an outage, so only the stale watcher alerts on it."""
    from scripts.swarm import outage

    if any(store.redis.exists(store.key(slug, mark)) for mark in HALTS):
        return True
    return any(record["kind"] == "ledger" for record in outage.open_records(slug))


def stale(beat: Heartbeat | None, now: int, since: int | None = None, limit: int = LIMIT_MS) -> list[str]:
    """since, when the judge began watching, is the earliest time anything is judged from."""
    if beat is None:
        if since is None or now - since < limit:
            return []
        return [f"no tick pass has completed in the {_minutes(now - since)} since the watch began"]
    evidence = []
    if now - max(beat.at, since or 0) >= limit:
        evidence.append(f"no tick pass has completed for {_minutes(now - beat.at)}, the last ended at {clock(beat.at)}")
    for name, observed in beat.observed.items():
        if observed is not None and beat.at - max(observed, since or 0) >= limit:
            evidence.append(f"the {name} it decides on was {_minutes(beat.at - observed)} old at its last pass")
    return evidence


def clock(at: int) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(at / 1000))


def _minutes(ms: int) -> str:
    minutes = ms // 60_000
    return "1 minute" if minutes == 1 else f"{minutes} minutes"
