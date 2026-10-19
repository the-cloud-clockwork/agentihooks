import functools
import sqlite3
from pathlib import Path

from scripts.swarm_ledger.repository import LedgerRepository, sqlite

DATABASE = sqlite.DATABASE
Error = sqlite3.Error
Missing = sqlite.Missing
fenced = sqlite.fenced
read_document = sqlite.read_document
read_ids = sqlite.read_ids
read_ledger = sqlite.read_ledger
read_ledgers = sqlite.read_ledgers
read_registry = sqlite.read_registry
read_slugs = sqlite.read_slugs


def build(path: Path | None = None, domain: object | None = None) -> LedgerRepository:
    return sqlite.SQLiteLedgerRepository(path, domain)


@functools.cache
def default() -> LedgerRepository:
    return build()
