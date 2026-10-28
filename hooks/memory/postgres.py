"""Postgres with pgvector behind the session catalog: the catalog's SQLite statements are translated, never forked.

Writers take one transaction scoped advisory lock, as SQLite's ``BEGIN IMMEDIATE`` serializes them; text columns
use the C collation so ordering matches SQLite byte order; chunk vectors are stored in a pgvector column.
"""

import re
import sqlite3
from collections.abc import Sequence

SCHEMES = ("postgresql://", "postgres://")
WRITER_LOCK = 0x5E55105
VECTOR_INSTALLED = "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
NO_VECTOR = "the session catalog database has no pgvector extension; install vector before opening it"
UNSUPPORTED = "this statement has no Postgres translation: "
SQLITE_ONLY = re.compile(r"\browid\b|\bsqlite_master\b|\bINSERT OR REPLACE\b|json_each\((?!\?\))")
JSON_FIELD = re.compile(r"json_extract\(([\w.]+), '\$\.(\w+)'\)")
JSON_LIST = "SELECT value FROM json_each(?)"
SELF_INCREMENT = re.compile(r"SET (\w+) = \1 \+ excluded\.\1")
TARGET = re.compile(r"INSERT INTO (\w+)")
DDL = (
    (re.compile(r"\bvector TEXT\b"), "vector vector"),
    (re.compile(r"\bTEXT\b"), 'TEXT COLLATE "C"'),
    (re.compile(r"\bINTEGER\b"), "BIGINT"),
    (re.compile(r"\bREAL\b"), "DOUBLE PRECISION"),
)


def selected(location: object) -> bool:
    return isinstance(location, str) and location.startswith(SCHEMES)


def translate(statement: str) -> str | None:
    if statement.startswith("PRAGMA"):
        return None
    if SQLITE_ONLY.search(statement):
        raise ValueError(UNSUPPORTED + statement[:80])
    if statement.startswith("CREATE TABLE"):
        for pattern, replacement in DDL:
            statement = pattern.sub(replacement, statement)
    statement = JSON_FIELD.sub(r"(\1::jsonb ->> '\2')", statement)
    statement = statement.replace(JSON_LIST, 'SELECT value COLLATE "C" FROM jsonb_array_elements_text(?::jsonb)')
    if statement.startswith("INSERT OR IGNORE INTO"):
        statement = statement.replace("INSERT OR IGNORE INTO", "INSERT INTO", 1) + " ON CONFLICT DO NOTHING"
    target = TARGET.match(statement)
    if target:
        statement = SELF_INCREMENT.sub(rf"SET \1 = {target.group(1)}.\1 + excluded.\1", statement)
    return statement.replace("%", "%%").replace("?", "%s")


class Connection:
    def __init__(self, raw) -> None:
        self.raw = raw

    @property
    def in_transaction(self) -> bool:
        from psycopg2 import extensions

        return self.raw.get_transaction_status() != extensions.TRANSACTION_STATUS_IDLE

    def execute(self, statement: str, parameters: Sequence = ()):
        import psycopg2

        translated = translate(statement)
        cursor = self.raw.cursor()
        try:
            if statement == "BEGIN IMMEDIATE":
                cursor.execute("BEGIN")
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (WRITER_LOCK,))
            elif translated is not None:
                cursor.execute(translated, tuple(parameters))
        except psycopg2.OperationalError as error:
            raise sqlite3.OperationalError(str(error)) from error
        except psycopg2.IntegrityError as error:
            raise sqlite3.IntegrityError(str(error)) from error
        return cursor

    def close(self) -> None:
        self.raw.close()


def connect(url: str) -> Connection:
    import psycopg2

    try:
        raw = psycopg2.connect(url)
    except psycopg2.OperationalError as error:
        raise sqlite3.OperationalError(str(error)) from error
    raw.autocommit = True
    connection = Connection(raw)
    try:
        installed = connection.execute(VECTOR_INSTALLED).fetchone()
    except sqlite3.OperationalError:
        raw.close()
        raise
    if installed is None:
        raw.close()
        raise sqlite3.OperationalError(NO_VECTOR)
    return connection
