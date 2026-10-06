"""The HOME bin: ledgers hidden from HOME, kept 30 days, then deleted for good.

A small ledger moves itself here once every item in it is done or after 7 days with no change.
"""

import json
import threading

import ledger_core as core
import ledger_media
import ledger_size

KEEP_DAYS = 30
IDLE_DAYS = 7
DAY_MS = 24 * 60 * 60 * 1000
LOCK = threading.Lock()


def bin_path():
    return core.LEDGER_DIR / ".bin.json"


def restored_path():
    return core.LEDGER_DIR / ".bin-restored.json"


def restored():
    try:
        found = json.loads(restored_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, int)} if isinstance(found, dict) else {}


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


def restore(slug, now=None):
    with LOCK:
        found = entries()
        if found.pop(slug, None) is None:
            return False
        _save(found)
        marks = restored()
        marks[slug] = core.now_ms() if now is None else now
        core.atomic_write(restored_path(), json.dumps(marks, indent=1, sort_keys=True))
        return True


def bin_closed(slug, closed_at, now=None):
    with LOCK:
        found, marks = entries(), restored()
        if slug in found or (slug in marks and marks[slug] >= closed_at):
            return False
        found[slug] = core.now_ms() if now is None else now
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
            ledger_media.purge(slug)
            del found[slug]
        if expired:
            _save(found)
    return expired


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


def due(state, now, restored_at=None):
    meta = state.get("_meta", {})
    changed = meta.get("updated_at") or meta.get("created_at") or 0
    if restored_at is not None and changed <= restored_at:
        return now - restored_at > IDLE_DAYS * DAY_MS
    return finished(state) or now - changed > IDLE_DAYS * DAY_MS


def auto_bin(now=None):
    now = core.now_ms() if now is None else now
    binned = []
    with LOCK, core.LOCK:
        found, marks = entries(), restored()
        for html_path in sorted(core.LEDGER_DIR.glob("*.html")):
            slug, json_path = html_path.stem, core.paths(html_path.stem)[1]
            if slug in found:
                continue
            try:
                state = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(state, dict) and ledger_size.size_of(state) == "small" and due(state, now, marks.get(slug)):
                found[slug] = now
                binned.append(slug)
        if binned:
            _save(found)
    return binned


def tidy(now=None):
    now = core.now_ms() if now is None else now
    return auto_bin(now), purge_expired(now)
