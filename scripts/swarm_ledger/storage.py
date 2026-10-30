import functools
import os
import sqlite3
from pathlib import Path

from scripts.swarm_ledger.repository import LedgerRepository, postgres, remote, sqlite

DATABASE = sqlite.DATABASE
BEGIN_IMMEDIATE = sqlite.BEGIN_IMMEDIATE
Error = sqlite3.Error
Missing = sqlite.Missing
Conflict = sqlite.Conflict


class Folder(sqlite.SQLiteLedgerRepository):
    adopts = False

    def _adopt(self, slug: str | None = None) -> None:
        """A store owner's folder never imports legacy ledger files."""


def owned(domain: object, path: Path | None = None) -> LedgerRepository:
    """The repository a process that owns the store serves: the shared database when its URL is set, else the
    ledger folder, never adopting legacy files."""
    address = shared()
    if path is None and address:
        return postgres.PostgresLedgerRepository(address, domain)
    return Folder(path, domain)


def shared() -> str | None:
    return postgres.url(os.environ)


def store(directory):
    """The shared database when its URL is set, else the ledger folder given."""
    address = shared()
    return postgres.Database(address) if address else directory


def fenced(directory, slug: str) -> bool:
    return sqlite.fenced(store(directory), slug)


def folder_frozen(directory) -> bool:
    return sqlite.frozen(directory)


def read_document(directory, slug: str) -> dict | None:
    return sqlite.read_document(store(directory), slug)


def read_ids(directory, slug: str, collection: str) -> tuple:
    return sqlite.read_ids(store(directory), slug, collection)


def read_ledger(directory, slug: str, *keys: str) -> dict | None:
    return sqlite.read_ledger(store(directory), slug, *keys)


def read_ledgers(directory, *keys: str) -> dict:
    return sqlite.read_ledgers(store(directory), *keys)


def read_registry(directory, name: str) -> dict:
    return sqlite.read_registry(store(directory), name)


def read_slugs(directory) -> list:
    return sqlite.read_slugs(store(directory))


def api() -> str | None:
    return os.environ.get(remote.URL_ENV) or None


def build(path: Path | None = None, domain: object | None = None) -> LedgerRepository:
    address, service = shared(), api()
    if path is None and service and not address:
        return remote.RemoteLedgerRepository(remote.Client(service, os.environ.get(remote.TOKEN_ENV, "")), domain)
    if path is None and address:
        return postgres.PostgresLedgerRepository(address, domain)
    return sqlite.SQLiteLedgerRepository(path, domain)


@functools.cache
def default() -> LedgerRepository:
    return build()
