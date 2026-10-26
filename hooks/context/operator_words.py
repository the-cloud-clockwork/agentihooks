"""The operator's own words in an append only log, indexed by normalized line hashes.

A relay to the ledger is accepted only when it quotes words recorded here for a master or planner of its swarm.
Swarm entries are mirrored into the shared swarm Redis, where a hosted ledger server reads them.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from fnmatch import fnmatchcase

from hooks.context import master_coding
from hooks.context.swarm_heartbeat import is_operator_prompt

TTL_SEC = 3600
KEPT = 50
_SHARED = []


def _dir():
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "operator_words"


def _path(name):
    return _dir() / (re.sub(r"[^A-Za-z0-9_.@-]", "_", name) + ".json")


def _norm(text):
    return " ".join(str(text).lower().split())


@contextmanager
def _store(migrate=True):
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
            "CREATE TABLE IF NOT EXISTS published ("
            "entry INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE);"
        )
        if migrate:
            _migrate(connection)
        with connection:
            yield connection
        _publish_pending(connection)
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


def _shared():
    from redis.exceptions import RedisError

    from scripts.swarm import store

    if not _SHARED:
        try:
            _SHARED.append(store.redis_client())
        except (RedisError, OSError):
            return None
    return _SHARED[0]


def _shared_key(*parts):
    from scripts.swarm.store import PREFIX

    return ":".join((PREFIX, "operator-words", *parts))


def _shared_call(action, default):
    from redis.exceptions import RedisError

    redis = _shared()
    if redis is None:
        return default
    try:
        return action(redis)
    except RedisError:
        return default


def _mirror(swarm, name, at, words):
    def push(redis):
        row = json.dumps({"swarm": swarm, "at": at, "words": words})
        with redis.pipeline() as pipe:
            pipe.sadd(_shared_key(), name).zadd(_shared_key(name, "entries"), {row: at}).execute()
        return True

    return _shared_call(push, False)


def _publish_pending(connection):
    rows = connection.execute(
        "SELECT id, swarm, name, at, words FROM entries WHERE swarm != '' "
        "AND id NOT IN (SELECT entry FROM published) ORDER BY at, id"
    ).fetchall()
    published = []
    for entry, swarm, name, at, words in rows:
        if not _mirror(swarm, name, at, words):
            break
        published.append((entry,))
    with connection:
        connection.executemany("INSERT OR IGNORE INTO published VALUES (?)", published)


def _shared_rows(name):
    def read(redis):
        return [
            json.loads(row)
            for row in [
                *redis.zrevrange(_shared_key(name, "entries"), 0, -1),
                *redis.lrange(_shared_key(name), 0, -1),
            ]
        ]

    return sorted(_shared_call(read, []), key=lambda row: row["at"], reverse=True)


def _shared_match(name, needle, since):
    for row in _shared_rows(name):
        if (since is None or row["at"] > since) and needle in _norm(row["words"]):
            return (row["words"],)
    return None


def _shared_forget(swarm):
    def drop(redis):
        for name in redis.smembers(_shared_key()):
            for row in _shared_rows(name):
                if row["swarm"] == swarm:
                    encoded = json.dumps(row)
                    redis.zrem(_shared_key(name, "entries"), encoded)
                    redis.lrem(_shared_key(name), 0, encoded)

    _shared_call(drop, None)


def _offline_swarm(name):
    from scripts.swarm.naming import legacy_slug, parse

    return None if parse(name) else legacy_slug(name)


def _legacy_swarms(paths, redis):
    from redis.exceptions import RedisError

    from scripts.swarm.naming import NameRegistry

    swarms = dict.fromkeys(paths)
    registry = NameRegistry(redis)
    for path in paths:
        if redis is None:
            swarms[path] = _offline_swarm(path.stem)
            continue
        try:
            # slug_of swallows a failed alias lookup; this read raises it so the name stays pending.
            redis.get(registry.key("alias", path.stem))
            swarms[path] = registry.slug_of(path.stem)
        except RedisError:
            break
    return swarms


def _migrate(connection):
    from hooks._redis import get_redis

    if connection.execute("SELECT 1 FROM legacy WHERE name = ''").fetchone():
        return
    swarms = _legacy_swarms([path for path in _dir().glob("*.json") if _load(path.stem)["rows"]], get_redis())
    clean = {}
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for path, swarm in swarms.items():
            if swarm is None:
                continue
            data = _load(path.stem)
            clean[path.stem] = {"sessions": data["sessions"]}
            if connection.execute("INSERT OR IGNORE INTO legacy VALUES (?)", (path.name,)).rowcount:
                for row in data["rows"]:
                    _append(connection, swarm, path.stem, row["at"], row["words"])
    for name, data in clean.items():
        _save(name, data)
    if None not in swarms.values():
        with connection:
            connection.execute("INSERT OR IGNORE INTO legacy VALUES ('')")


def _line_match(connection: sqlite3.Connection, name: str, needle: str, since: float | None) -> tuple | None:
    return connection.execute(
        "SELECT entries.words FROM lines JOIN entries ON entries.id = lines.entry "
        "WHERE lines.name = ? AND lines.hash = ? AND (? IS NULL OR entries.at > ?) "
        "ORDER BY entries.at DESC, entries.id DESC LIMIT 1",
        (name, _hash(needle), since, since),
    ).fetchone()


def forget(swarm: str) -> None:
    with _store() as connection:
        connection.execute("DELETE FROM entries WHERE swarm = ?", (swarm,))
    _shared_forget(swarm)


def _load(name):
    try:
        data = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        data = {}
    return {"sessions": data.get("sessions", []), "rows": data.get("rows", [])}


def recorded(pattern: str) -> list[str]:
    with _store() as connection:
        names = connection.execute("SELECT DISTINCT name FROM entries").fetchall()
    shared = _shared_call(lambda redis: redis.smembers(_shared_key()), set())
    return sorted(name for name in {name for (name,) in names} | shared if fnmatchcase(name, pattern))


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
    with _store(migrate=False) as connection:
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
                "AND (? IS NULL OR at > ?) ORDER BY at DESC, id DESC LIMIT 1",
                (name, needle, since, since),
            ).fetchone()
    row = row or _shared_match(name, needle, since)
    return row[0] if row else ""


def _opening(name, session):
    """True once per session: the first prompt of a swarm agent is its launch or handoff prompt."""
    data = _load(name)
    if not session or session in data["sessions"]:
        return False
    data["sessions"] = (data["sessions"] + [session])[-KEPT:]
    _save(name, data)
    return True


def _speaker(env, session):
    seat = master_coding.seat(session)
    if seat:
        return seat.get("name"), seat.get("slug"), True
    return env.get("AGENTIHOOKS_AGENT_NAME"), env.get("AGENTIHOOKS_SWARM"), False


def heard_prompt(prompt, environ=None, now=None, session=""):
    env = os.environ if environ is None else environ
    name, slug, seated = _speaker(env, session)
    if not name or not is_operator_prompt(prompt, slug):
        return False
    now = time.time() if now is None else now
    if slug and not seated and _opening(name, session):
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
    name, slug, _ = _speaker(env, payload.get("session_id"))
    return record(name, _answer_words(payload), now, slug)


def heard(payload, environ=None, now=None):
    if "prompt" in payload:
        return heard_prompt(payload["prompt"], environ, now, payload.get("session_id"))
    return heard_answer(payload, environ, now)


def typed(payload, environ=None, now=None):
    """True when the payload holds the operator's own words: a named session records them, an unnamed one has no launch prompt."""
    env = os.environ if environ is None else environ
    if env.get("AGENTIHOOKS_AGENT_NAME") or master_coding.seat(payload.get("session_id")):
        return heard(payload, env, now)
    return is_operator_prompt(payload.get("prompt", ""), env.get("AGENTIHOOKS_SWARM"))
