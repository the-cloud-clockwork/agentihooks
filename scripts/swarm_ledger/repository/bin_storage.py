from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import ledger_bin as domain
import ledger_core as core
import ledger_media

from scripts.swarm_ledger import ports, request_metrics

from .sqlite import BEGIN, BEGIN_IMMEDIATE


def _repository():
    from scripts.swarm_ledger import storage

    from . import legacy

    repository = storage.default()
    legacy.adopt_bin(repository)
    return repository


@contextmanager
def _transaction():
    repository = _repository()
    with request_metrics.locked(domain.LOCK), repository.connect() as connection, connection:
        request_metrics.begin(connection, BEGIN_IMMEDIATE)
        yield repository, connection


@contextmanager
def _reading() -> Iterator[tuple[Any, Any]]:
    """A read snapshot of the shared repository that takes no writer lock."""
    repository = _repository()
    with repository.connect() as connection, connection:
        connection.execute(BEGIN)
        yield repository, connection


def _expired(found: dict[str, Any], now: int) -> list[str]:
    """Binned ledgers kept past the bin's days, in name order."""
    return sorted(
        slug for slug, at in found.items() if isinstance(at, int) and now - at > domain.KEEP_DAYS * domain.DAY_MS
    )


def _due(repository: Any, connection: Any, now: int) -> tuple[dict[str, Any], list[str]]:
    """The bin registry and the small ledgers it does not hold that are finished or idle."""
    found, marks = repository.registry("bin", connection), repository.registry("restored", connection)
    return found, [
        s["slug"]
        for s in repository.summaries(connection)
        if s["slug"] not in found
        and s["size"] == "small"
        and domain.idle_due(s["finished"], s["updated_at"] or s["created_at"] or 0, now, marks.get(s["slug"]))
    ]


def _registry(name):
    from scripts.swarm_ledger import storage

    return storage.read_registry(_repository().directory, name)


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


def purge_expired(now: int | None = None) -> list[str]:
    now = core.now_ms() if now is None else now
    with _reading() as (repository, connection):
        if not _expired(repository.registry("bin", connection), now):
            return []
    with _transaction() as (repository, connection):
        found = repository.registry("bin", connection)
        expired = _expired(found, now)
        for slug in expired:
            repository.purge(slug, connection)
            del found[slug]
        if expired:
            repository.save_registry(connection, "bin", found)
    for slug in expired:
        ledger_media.purge(slug)
        ports.hooks().operator_words.forget(slug)
    return expired


def auto_bin(now: int | None = None) -> list[str]:
    now = core.now_ms() if now is None else now
    with _reading() as (repository, connection):
        _, binned = _due(repository, connection, now)
        if not binned:
            return []
    with _transaction() as (repository, connection):
        found, binned = _due(repository, connection, now)
        for slug in binned:
            found[slug] = now
        if binned:
            repository.save_registry(connection, "bin", found)
    return sorted(binned)
