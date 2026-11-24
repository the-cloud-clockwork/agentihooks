from typing import Any

from scripts.swarm_ledger.repository.rows import load, read_rows
from scripts.swarm_ledger.repository.sqlite import partial_rows


def apply_claim(services: Any, slug: str) -> Any:
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return services.repository.current(slug).state


def hook_state(services: Any, slug: str) -> Any:
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return services.repository.get_document(slug)


def update(repository: Any, connection: Any, slug: str) -> Any:
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return repository._entry(connection, slug)


def persist(entry: Any) -> Any:
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return entry.for_write()


def placed(connection: Any, slug: str) -> Any:
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return load(connection, slug)


def admitted(connection: Any, slug: str) -> Any:
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return read_rows(connection, slug)


class Publisher:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def apply_task_update(self, slug: str) -> Any:
        # ruleid: ledger-task-update-narrow-reads-whole-state
        return self.inner.current(slug)


class Keyed:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def apply_claim(self, slug: str) -> Any:
        # ok: ledger-task-update-narrow-reads-whole-state
        return self.repository.read(slug, "_meta.warnings")

    def update(self, connection: Any, slug: str, keys: Any) -> Any:
        # ok: ledger-task-update-narrow-reads-whole-state
        return partial_rows(connection, slug, keys)


def apply(services: Any, slug: str) -> Any:
    # ok: ledger-task-update-narrow-reads-whole-state
    return services.repository.current(slug).state
