from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from hooks import config

DEFAULT_TTL_S = 7 * 24 * 3600


def cache_path() -> Path:
    return config.AGENTIHOOKS_HOME / "classifier" / "answers.json"


def ttl_s() -> float:
    return float(os.environ.get("AGENTIHOOKS_FILTER_CACHE_TTL_S", DEFAULT_TTL_S))


def key(intent: str, question: str, finding: str, reason: str, context: str, models: tuple) -> str:
    return hashlib.sha256(json.dumps([intent, question, finding, reason, context, list(models)]).encode()).hexdigest()


def _live() -> dict[str, dict]:
    try:
        entries = json.loads(cache_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(entries, dict):
        return {}
    oldest = time.time() - ttl_s()
    return {name: entry for name, entry in entries.items() if _fresh(entry, oldest)}


def _fresh(entry: object, oldest: float) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("answer"), bool):
        return False
    at = entry.get("at")
    return isinstance(at, (int, float)) and at > oldest


def lookup(keys: list[str]) -> dict[str, bool]:
    entries = _live()
    return {name: entries[name]["answer"] for name in keys if name in entries}


def store(answers: dict[str, bool]) -> None:
    if not answers:
        return
    now = time.time()
    entries = _live()
    entries.update({name: {"answer": answer, "at": now} for name, answer in answers.items()})
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(entries))
    os.replace(temporary, path)
