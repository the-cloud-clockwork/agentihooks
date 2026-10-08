"""The HOME bin: ledgers hidden from HOME, kept 30 days, then deleted for good.

A small ledger moves itself here once every item in it is done or after 7 days with no change.
"""

import threading

import ledger_core as core

KEEP_DAYS = 30
IDLE_DAYS = 7
DAY_MS = 24 * 60 * 60 * 1000
LOCK = threading.Lock()


def restored():
    from scripts.swarm_ledger.repository import bin_storage

    return bin_storage.restored()


def entries():
    from scripts.swarm_ledger.repository import bin_storage

    return bin_storage.entries()


def days_left(deleted_at, now):
    return max(0, -((now - deleted_at - KEEP_DAYS * DAY_MS) // DAY_MS))


def delete(slug, now=None):
    from scripts.swarm_ledger.repository import repository

    return repository.delete(slug, now)


def restore(slug, now=None):
    from scripts.swarm_ledger.repository import repository

    return repository.restore(slug, now)


def bin_closed(slug, closed_at, now=None):
    from scripts.swarm_ledger.repository import bin_storage

    return bin_storage.bin_closed(slug, closed_at, now)


def purge_expired(now=None):
    from scripts.swarm_ledger.repository import bin_storage

    return bin_storage.purge_expired(now)


def _answered(question):
    return any(not a.get("deleted") and a.get("text", "").strip() for a in question.get("answers", []))


def finished(doc):
    items = [i for key in ("phases", "followups", "tasks") for i in doc.get(key, []) if not i.get("out_of_scope")]
    questions = [q for q in doc.get("questions", []) if not q.get("out_of_scope")]
    return (
        bool(items)
        and all(i.get("done") is True or i.get("state") == "done" for i in items)
        and all(_answered(q) for q in questions)
    )


def idle_due(done, changed, now, restored_at=None):
    if restored_at is not None and changed <= restored_at:
        return now - restored_at > IDLE_DAYS * DAY_MS
    return done or now - changed > IDLE_DAYS * DAY_MS


def auto_bin(now=None):
    from scripts.swarm_ledger.repository import bin_storage

    return bin_storage.auto_bin(now)


def tidy(now=None):
    now = core.now_ms() if now is None else now
    return auto_bin(now), purge_expired(now)
