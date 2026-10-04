"""The HOME bin: ledgers hidden from HOME, kept 30 days, then deleted for good."""

import json
import threading

import ledger_core as core

KEEP_DAYS = 30
DAY_MS = 24 * 60 * 60 * 1000
LOCK = threading.Lock()


def bin_path():
    return core.LEDGER_DIR / ".bin.json"


def entries():
    try:
        found = json.loads(bin_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, int)} if isinstance(found, dict) else {}


def _save(found):
    core.atomic_write(bin_path(), json.dumps(found, indent=1, sort_keys=True))


def days_left(deleted_at, now):
    return max(0, -((now - deleted_at - KEEP_DAYS * DAY_MS) // DAY_MS))


def delete(slug, now=None):
    with LOCK:
        found = entries()
        found.setdefault(slug, core.now_ms() if now is None else now)
        _save(found)


def restore(slug):
    with LOCK:
        found = entries()
        if found.pop(slug, None) is None:
            return False
        _save(found)
        return True


def purge_expired(now=None):
    now = core.now_ms() if now is None else now
    with LOCK:
        found = entries()
        expired = sorted(slug for slug, at in found.items() if now - at > KEEP_DAYS * DAY_MS)
        for slug in expired:
            for path in core.paths(slug):
                path.unlink(missing_ok=True)
            del found[slug]
        if expired:
            _save(found)
    return expired
