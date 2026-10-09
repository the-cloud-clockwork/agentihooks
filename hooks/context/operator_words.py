"""The operator's own words in an append only log, indexed by normalized line hashes.

A relay to the ledger is accepted only when it quotes words recorded here for a master or planner of its swarm.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from fnmatch import fnmatchcase

from hooks.context.swarm_heartbeat import is_operator_prompt

TTL_SEC = 3600
KEPT = 50


def _dir():
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "operator_words"


def _path(name):
    return _dir() / (re.sub(r"[^A-Za-z0-9_.@-]", "_", name) + ".json")


def _norm(text):
    return " ".join(str(text).lower().split())


@contextmanager
def _store():
    path = _dir() / "words.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS entries ("
            "id INTEGER PRIMARY KEY, swarm TEXT NOT NULL, name TEXT NOT NULL, "
            "at REAL NOT NULL, words TEXT NOT NULL, norm TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS entry_swarm ON entries(swarm);"
            "CREATE INDEX IF NOT EXISTS entry_name ON entries(name);"
            "CREATE TABLE IF NOT EXISTS lines ("
            "name TEXT NOT NULL, hash TEXT NOT NULL, entry INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE);"
            "CREATE INDEX IF NOT EXISTS line_hash ON lines(name, hash);"
            "CREATE TABLE IF NOT EXISTS legacy (name TEXT PRIMARY KEY);"
        )
        _migrate(connection)
        with connection:
            yield connection
    finally:
        connection.close()


def _hash(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def _append(connection, swarm, name, at, words):
    entry = connection.execute(
        "INSERT INTO entries (swarm, name, at, words, norm) VALUES (?, ?, ?, ?, ?)",
        (swarm, name, at, words, _norm(words)),
    ).lastrowid
    connection.executemany(
        "INSERT INTO lines (name, hash, entry) VALUES (?, ?, ?)",
        [(name, _hash(_norm(line)), entry) for line in words.splitlines()],
    )


def _legacy_swarm(name):
    from hooks._redis import get_redis
    from scripts.swarm.naming import NameRegistry, legacy_slug, parse

    redis = get_redis()
    swarm = NameRegistry(redis).slug_of(name) if redis is not None else legacy_slug(name)
    return None if not swarm and parse(name) else swarm


def _migrate(connection):
    if connection.execute("SELECT 1 FROM legacy WHERE name = ''").fetchone():
        return
    clean = {}
    pending = False
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for path in _dir().glob("*.json"):
            data = _load(path.stem)
            if not data["rows"]:
                continue
            swarm = _legacy_swarm(path.stem)
            if swarm is None:
                pending = True
                continue
            clean[path.stem] = {"sessions": data["sessions"]}
            if connection.execute("INSERT OR IGNORE INTO legacy VALUES (?)", (path.name,)).rowcount:
                for row in data["rows"]:
                    _append(connection, swarm, path.stem, row["at"], row["words"])
    for name, data in clean.items():
        _save(name, data)
    if not pending:
        with connection:
            connection.execute("INSERT OR IGNORE INTO legacy VALUES ('')")


def _line_match(connection: sqlite3.Connection, name: str, needle: str, since: float | None) -> tuple | None:
    return connection.execute(
        "SELECT entries.words FROM lines JOIN entries ON entries.id = lines.entry "
        "WHERE lines.name = ? AND lines.hash = ? AND (? IS NULL OR entries.at > ?) "
        "ORDER BY entries.id DESC LIMIT 1",
        (name, _hash(needle), since, since),
    ).fetchone()


def forget(swarm: str) -> None:
    with _store() as connection:
        connection.execute("DELETE FROM entries WHERE swarm = ?", (swarm,))


def _load(name):
    try:
        data = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        data = {}
    return {"sessions": data.get("sessions", []), "rows": data.get("rows", [])}


def recorded(pattern: str) -> list[str]:
    with _store() as connection:
        names = connection.execute("SELECT DISTINCT name FROM entries").fetchall()
    return sorted(name for (name,) in names if fnmatchcase(name, pattern))


def _save(name, data):
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def record(name: str, words: str, now: float | None = None, swarm: str = "") -> bool:
    now = time.time() if now is None else now
    text = str(words or "").strip()
    if not (name and text):
        return False
    with _store() as connection:
        _append(connection, swarm or "", name, now, text)
    return True


def matching(name: str, quote: str, now: float | None = None, within: float | None = TTL_SEC) -> str:
    needle = _norm(quote)
    if not needle:
        return ""
    now = time.time() if now is None else now
    since = None if within is None else now - within
    with _store() as connection:
        row = _line_match(connection, name, needle, since)
        if row is None:
            row = connection.execute(
                "SELECT words FROM entries WHERE name = ? AND instr(norm, ?) > 0 "
                "AND (? IS NULL OR at > ?) ORDER BY id DESC LIMIT 1",
                (name, needle, since, since),
            ).fetchone()
    return row[0] if row else ""


def _opening(name, session):
    """True once per session: the first prompt of a swarm agent is its launch or handoff prompt."""
    with _store():
        pass
    data = _load(name)
    if not session or session in data["sessions"]:
        return False
    data["sessions"] = (data["sessions"] + [session])[-KEPT:]
    _save(name, data)
    return True


def heard_prompt(prompt, environ=None, now=None, session=""):
    env = os.environ if environ is None else environ
    name, slug = env.get("AGENTIHOOKS_AGENT_NAME"), env.get("AGENTIHOOKS_SWARM")
    if not name or not is_operator_prompt(prompt, slug):
        return False
    now = time.time() if now is None else now
    if slug and _opening(name, session):
        return False
    return record(name, prompt, now, slug)


def _answer_words(payload):
    for source in (payload.get("tool_response"), payload.get("tool_input")):
        if isinstance(source, dict) and source.get("answers"):
            notes = [a.get("notes") for a in (source.get("annotations") or {}).values() if isinstance(a, dict)]
            return "\n".join(str(w) for w in [*source["answers"].values(), *notes] if w)
    return ""


def heard_answer(payload, environ=None, now=None):
    if payload.get("tool_name") != "AskUserQuestion":
        return False
    env = os.environ if environ is None else environ
    return record(env.get("AGENTIHOOKS_AGENT_NAME"), _answer_words(payload), now, env.get("AGENTIHOOKS_SWARM"))


def heard(payload, environ=None, now=None):
    if "prompt" in payload:
        return heard_prompt(payload["prompt"], environ, now, payload.get("session_id"))
    return heard_answer(payload, environ, now)


def typed(payload, environ=None, now=None):
    """True when the payload holds the operator's own words: a named session records them, an unnamed one has no launch prompt."""
    env = os.environ if environ is None else environ
    if env.get("AGENTIHOOKS_AGENT_NAME"):
        return heard(payload, env, now)
    return is_operator_prompt(payload.get("prompt", ""), env.get("AGENTIHOOKS_SWARM"))
