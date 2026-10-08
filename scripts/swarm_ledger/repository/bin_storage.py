from contextlib import contextmanager

import ledger_bin as domain
import ledger_core as core
import ledger_media

from .sqlite import BEGIN_IMMEDIATE, read_registry


def _repository():
    from . import legacy, repository

    legacy.adopt_bin(repository)
    return repository


@contextmanager
def _transaction():
    repository = _repository()
    with domain.LOCK, repository.connect() as connection, connection:
        connection.execute(BEGIN_IMMEDIATE)
        yield repository, connection


def _registry(name):
    return read_registry(_repository().directory, name)


def restored():
    return {k: v for k, v in _registry("restored").items() if isinstance(v, int)}


def entries():
    return {k: v for k, v in _registry("bin").items() if isinstance(v, int)}


def registries():
    return {"bin": _registry("bin"), "restored": _registry("restored")}


def delete(slug, now=None):
    with _transaction() as (repository, connection):
        found = repository.registry("bin", connection)
        found.setdefault(slug, core.now_ms() if now is None else now)
        repository.save_registry(connection, "bin", found)


def restore(slug, now=None):
    with _transaction() as (repository, connection):
        found = repository.registry("bin", connection)
        if found.pop(slug, None) is None:
            return False
        repository.save_registry(connection, "bin", found)
        marks = repository.registry("restored", connection)
        marks[slug] = core.now_ms() if now is None else now
        repository.save_registry(connection, "restored", marks)
        return True


def bin_closed(slug, closed_at, now=None):
    with _transaction() as (repository, connection):
        found, marks = repository.registry("bin", connection), repository.registry("restored", connection)
        if slug in found or (slug in marks and marks[slug] >= closed_at):
            return False
        found[slug] = core.now_ms() if now is None else now
        repository.save_registry(connection, "bin", found)
        return True


def purge_expired(now=None):
    now = core.now_ms() if now is None else now
    with _transaction() as (repository, connection):
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
    with _transaction() as (repository, connection):
        found, marks = repository.registry("bin", connection), repository.registry("restored", connection)
        binned = [
            s["slug"]
            for s in repository.summaries(connection)
            if s["slug"] not in found
            and s["size"] == "small"
            and domain.idle_due(s["finished"], s["updated_at"] or s["created_at"] or 0, now, marks.get(s["slug"]))
        ]
        for slug in binned:
            found[slug] = now
        if binned:
            repository.save_registry(connection, "bin", found)
    return sorted(binned)
