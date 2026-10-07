import json

import ledger_bin as domain
import ledger_core as core
import ledger_media
import ledger_size


def restored():
    try:
        found = json.loads(domain.restored_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, int)} if isinstance(found, dict) else {}


def registries():
    found = {}
    for name, path in (("bin", domain.bin_path()), ("restored", domain.restored_path())):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        found[name] = value if isinstance(value, dict) else {}
    return found


def entries():
    try:
        found = json.loads(domain.bin_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, int)} if isinstance(found, dict) else {}


def _save(found):
    core.atomic_write(domain.bin_path(), json.dumps(found, indent=1, sort_keys=True))


def delete(slug, now=None):
    from .shadow import persist_lifecycle, storage_lock

    with domain.LOCK, storage_lock(core.LEDGER_DIR):
        found = entries()
        found.setdefault(slug, core.now_ms() if now is None else now)
        _save(found)
        persist_lifecycle(core.LEDGER_DIR)


def restore(slug, now=None):
    from .shadow import persist_lifecycle, storage_lock

    with domain.LOCK, storage_lock(core.LEDGER_DIR):
        found = entries()
        if found.pop(slug, None) is None:
            return False
        _save(found)
        marks = restored()
        marks[slug] = core.now_ms() if now is None else now
        core.atomic_write(domain.restored_path(), json.dumps(marks, indent=1, sort_keys=True))
        persist_lifecycle(core.LEDGER_DIR)
        return True


def bin_closed(slug, closed_at, now=None):
    from .shadow import persist_lifecycle, storage_lock

    with domain.LOCK, storage_lock(core.LEDGER_DIR):
        found, marks = entries(), restored()
        if slug in found or (slug in marks and marks[slug] >= closed_at):
            return False
        found[slug] = core.now_ms() if now is None else now
        _save(found)
        persist_lifecycle(core.LEDGER_DIR)
        return True


def purge_expired(now=None):
    from .shadow import persist_lifecycle, storage_lock

    now = core.now_ms() if now is None else now
    with domain.LOCK, storage_lock(core.LEDGER_DIR):
        found = entries()
        expired = sorted(slug for slug, at in found.items() if now - at > domain.KEEP_DAYS * domain.DAY_MS)
        for slug in expired:
            for path in core.paths(slug):
                path.unlink(missing_ok=True)
            ledger_media.purge(slug)
            del found[slug]
        if expired:
            _save(found)
            persist_lifecycle(core.LEDGER_DIR, expired)
    return expired


def auto_bin(now=None):
    from .shadow import persist_lifecycle, storage_lock

    now = core.now_ms() if now is None else now
    binned = []
    with domain.LOCK, core.LOCK, storage_lock(core.LEDGER_DIR):
        found, marks = entries(), restored()
        for html_path in sorted(core.LEDGER_DIR.glob("*.html")):
            slug, json_path = html_path.stem, core.paths(html_path.stem)[1]
            if slug in found:
                continue
            try:
                state = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                isinstance(state, dict)
                and ledger_size.size_of(state) == "small"
                and domain.due(state, now, marks.get(slug))
            ):
                found[slug] = now
                binned.append(slug)
        if binned:
            _save(found)
            persist_lifecycle(core.LEDGER_DIR)
    return binned
