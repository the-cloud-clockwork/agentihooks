import ledger_bin as domain
import ledger_core as core
import ledger_media


def _repository():
    from . import legacy, repository

    legacy.adopt(repository, "")
    return repository


def _mark(name, entries_of, now=None):
    repository = _repository()
    with domain.LOCK, repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        found = repository.registry(name, connection)
        result = entries_of(found, repository, connection)
        repository.save_registry(connection, name, found)
        return result


def restored():
    return {k: v for k, v in _repository().registry("restored").items() if isinstance(v, int)}


def entries():
    return {k: v for k, v in _repository().registry("bin").items() if isinstance(v, int)}


def registries():
    repository = _repository()
    return {"bin": repository.registry("bin"), "restored": repository.registry("restored")}


def delete(slug, now=None):
    _mark("bin", lambda found, *_: found.setdefault(slug, core.now_ms() if now is None else now))


def restore(slug, now=None):
    repository = _repository()
    with domain.LOCK, repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        found = repository.registry("bin", connection)
        if found.pop(slug, None) is None:
            return False
        repository.save_registry(connection, "bin", found)
        marks = repository.registry("restored", connection)
        marks[slug] = core.now_ms() if now is None else now
        repository.save_registry(connection, "restored", marks)
        return True


def bin_closed(slug, closed_at, now=None):
    repository = _repository()
    with domain.LOCK, repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        found, marks = repository.registry("bin", connection), repository.registry("restored", connection)
        if slug in found or (slug in marks and marks[slug] >= closed_at):
            return False
        found[slug] = core.now_ms() if now is None else now
        repository.save_registry(connection, "bin", found)
        return True


def purge_expired(now=None):
    now = core.now_ms() if now is None else now
    repository = _repository()
    with domain.LOCK, repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        found = repository.registry("bin", connection)
        expired = sorted(
            slug for slug, at in found.items() if isinstance(at, int) and now - at > domain.KEEP_DAYS * domain.DAY_MS
        )
        for slug in expired:
            repository.purge(slug, connection)
            del found[slug]
        if expired:
            repository.save_registry(connection, "bin", found)
    for slug in expired:
        ledger_media.purge(slug)
    return expired


def auto_bin(now=None):
    now = core.now_ms() if now is None else now
    repository = _repository()
    summaries = repository.summaries()
    with domain.LOCK, repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        found, marks = repository.registry("bin", connection), repository.registry("restored", connection)
        binned = [
            s["slug"]
            for s in summaries
            if s["slug"] not in found
            and s["size"] == "small"
            and domain.idle_due(s["finished"], s["updated_at"] or s["created_at"] or 0, now, marks.get(s["slug"]))
        ]
        for slug in binned:
            found[slug] = now
        if binned:
            repository.save_registry(connection, "bin", found)
    return sorted(binned)
