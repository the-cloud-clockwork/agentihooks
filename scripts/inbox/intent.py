"""Intent grouping for the master inbox pool behind MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS: a waiting item goes to the
live master whose recent items share its intent. Any classifier error, timeout or low verdict leaves the item to round
robin."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from hooks.classifier import DecisionRequest, YesNo, decide
from scripts.inbox.store import Item

if TYPE_CHECKING:
    from scripts.inbox.pool import MasterPool

FLAG = "MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS"
TIMEOUT_ENV = "MASTER_INBOX_CLASSIFIER_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 3.0
PURPOSE = "master-inbox-intent"
RECENT = 15
LINE_CHARS = 120
MATCH = 0.7
MAX_WORKERS = 16
CACHE_TTL_S = 3600
RECENT_TTL_S = 86400
TOKENS = "tokens"
SAME = YesNo(
    "Does the new item share its intent with the items this master handled recently?",
    true="the new item continues the same work, task or question as one of the recent items",
    false="the new item is about different work",
)


def enabled() -> bool:
    value = os.environ.get(FLAG)
    return value is None or value.strip().lower() in ("1", "true", "yes", "on")


def timeout_s() -> float:
    try:
        return float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip()[:LINE_CHARS] if lines else ""


def request(line: str, recent: list[str]) -> DecisionRequest:
    return DecisionRequest({"new": line, "recent": recent}, {"same": SAME})


def remember(pipe, pool: MasterPool, slug: str, seat: str, item: Item) -> None:
    """pipe is in MULTI: the first line of an item handed to seat joins that master's recent items."""
    key = pool.key(slug, "recent", seat)
    pipe.lpush(key, first_line(item.text))
    pipe.ltrim(key, 0, RECENT - 1)
    pipe.expire(key, RECENT_TTL_S)


def _digest(recent: list[str]) -> str:
    return hashlib.sha256(json.dumps(recent).encode()).hexdigest()


def _cached(cached: dict, field: str) -> float | None:
    try:
        return float(cached[field])
    except (KeyError, ValueError):
        return None


def _verdict(redis, asked: DecisionRequest, cache: str, field: str) -> float:
    """A verdict that lands after the deadline is still cached for the next pass."""
    verdict = decide(asked.state, asked.questions, purpose=PURPOSE, fallbacks=()).answers["same"].noul
    redis.hset(cache, field, verdict)
    redis.expire(cache, CACHE_TTL_S)
    return verdict


def _ask_all(redis, asks: dict) -> dict:
    """One verdict per item and master, all in parallel under one deadline; a failed or late ask has none."""
    if not asks:
        return {}
    executor = ThreadPoolExecutor(min(len(asks), MAX_WORKERS))
    futures = {executor.submit(_verdict, redis, *ask): pair for pair, ask in asks.items()}
    done, _ = wait(futures, timeout=timeout_s())
    return {futures[future]: future.result() for future in done if future.exception() is None}


def routes(pool: MasterPool, slug: str, items: list[Item], live: list[str]) -> dict[str, str | None]:
    """The master each item's intent matches, None for round robin; the estimated tokens sent add to the item's cache."""
    histories = {seat: pool.redis.lrange(pool.key(slug, "recent", seat), 0, -1) for seat in live}
    histories = {seat: recent for seat, recent in histories.items() if recent}
    verdicts, asks = {}, {}
    for item in items:
        cache = pool.key(slug, "intent", item.id)
        cached = pool.redis.hgetall(cache)
        sent = 0
        for seat, recent in histories.items():
            field = f"{seat}|{_digest(recent)}"
            if (verdict := _cached(cached, field)) is not None:
                verdicts[item.id, seat] = verdict
                continue
            asked = request(first_line(item.text), recent)
            asks[item.id, seat] = (asked, cache, field)
            sent += asked.estimated_tokens()
        if sent:
            pool.redis.hincrby(cache, TOKENS, sent)
            pool.redis.expire(cache, CACHE_TTL_S)
    verdicts = verdicts | _ask_all(pool.redis, asks)
    return {item.id: _best(item.id, histories, verdicts) for item in items}


def _best(item_id: str, histories: dict, verdicts: dict) -> str | None:
    matched = [(verdicts[item_id, seat], seat) for seat in histories if verdicts.get((item_id, seat), 0) >= MATCH]
    if matched:
        return max(matched)[1]
    return None
