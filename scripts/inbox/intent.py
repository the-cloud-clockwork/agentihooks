"""Intent grouping for the master inbox pool behind MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS: a waiting item goes to the
live master whose recent items share its intent. Any classifier error, timeout or low verdict leaves the item to round
robin."""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from hooks.classifier import DecisionRequest, YesNo, decide

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
SAME = YesNo(
    "Does the new item share its intent with the items this master handled recently?",
    true="the new item continues the same work, task or question as one of the recent items",
    false="the new item is about different work",
)


@dataclass(frozen=True)
class Route:
    seat: str
    tokens: int


def enabled() -> bool:
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip()[:LINE_CHARS] if lines else ""


def request(line: str, recent: list[str]) -> DecisionRequest:
    return DecisionRequest({"new": line, "recent": recent}, {"same": SAME})


def remember(pipe, pool, slug: str, seat: str, item) -> None:
    """pipe is in MULTI: the first line of an item handed to seat joins that master's recent items."""
    key = pool.key(slug, "recent", seat)
    pipe.lpush(key, first_line(item.text))
    pipe.ltrim(key, 0, RECENT - 1)
    pipe.expire(key, RECENT_TTL_S)


def _digest(recent: list[str]) -> str:
    return hashlib.sha256(json.dumps(recent).encode()).hexdigest()[:16]


def _verdict(asked: DecisionRequest) -> float:
    return decide(asked.state, asked.questions, purpose=PURPOSE, fallbacks=()).answers["same"].noul


def _ask_all(redis, asks: dict) -> dict:
    """One verdict per item and master, all in parallel under one deadline; a failed or late ask has none."""
    if not asks:
        return {}
    timeout = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    executor = ThreadPoolExecutor(max_workers=min(len(asks), MAX_WORKERS))
    futures = {executor.submit(_verdict, asked): pair for pair, (asked, _, _) in asks.items()}
    done, _ = wait(futures, timeout=timeout)
    executor.shutdown(wait=False, cancel_futures=True)
    verdicts = {}
    for future in done:
        if future.exception() is None:
            pair = futures[future]
            verdicts[pair] = future.result()
            _, cache, field = asks[pair]
            redis.hset(cache, field, verdicts[pair])
            redis.expire(cache, CACHE_TTL_S)
    return verdicts


def routes(pool, slug: str, items: list, live: list[str]) -> dict[str, Route]:
    """The master each item's intent matches, '' for round robin, and the estimated tokens sent for it."""
    histories = {seat: pool.redis.lrange(pool.key(slug, "recent", seat), 0, -1) for seat in live}
    histories = {seat: recent for seat, recent in histories.items() if recent}
    verdicts, asks, tokens = {}, {}, {}
    for item in items:
        cache = pool.key(slug, "intent", item.id)
        cached = pool.redis.hgetall(cache)
        tokens[item.id] = 0
        for seat, recent in histories.items():
            field = f"{seat}|{_digest(recent)}"
            if field in cached:
                verdicts[item.id, seat] = float(cached[field])
                continue
            asked = request(first_line(item.text), recent)
            asks[item.id, seat] = (asked, cache, field)
            tokens[item.id] += asked.estimated_tokens()
    verdicts |= _ask_all(pool.redis, asks)
    return {item.id: Route(_best(item.id, histories, verdicts), tokens[item.id]) for item in items}


def _best(item_id: str, histories: dict, verdicts: dict) -> str:
    matched = [(verdicts[item_id, seat], seat) for seat in histories if verdicts.get((item_id, seat), 0) >= MATCH]
    return max(matched)[1] if matched else ""
