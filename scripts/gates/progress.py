"""The one progress signal: each swarm agent's last outcome and the talk writes it made since, kept in Redis."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

OUTCOME_COMMANDS = (
    ("pushed", re.compile(r"(?:^|[;&|(\n]\s*)git(?:\s+-C\s+\S+)?\s+push\b")),
    ("pull request opened", re.compile(r"(?:^|[;&|(\n]\s*)gh\s+pr\s+create\b")),
)


@dataclass(frozen=True)
class Mark:
    outcome_at: int = 0
    outcome: str = ""
    talk: int = 0


def outcome_of(command: str) -> str:
    return next((kind for kind, pattern in OUTCOME_COMMANDS if command and pattern.search(command)), "")


class Progress:
    def __init__(self, redis: Any, slug: str) -> None:
        self.redis, self.slug = redis, slug

    def key(self, agent: str) -> str:
        from scripts.hive import scope

        return f"{scope.readings(self.redis, self.slug, agent)}:{self.slug}:progress:{agent}"

    def read(self, agent: str) -> Mark:
        raw = self.redis.hgetall(self.key(agent))
        return Mark(int(raw.get("outcome_at") or 0), raw.get("outcome", ""), int(raw.get("talk") or 0))

    def talk(self, agent: str) -> int:
        return int(self.redis.hincrby(self.key(agent), "talk"))

    def outcome(self, agent: str, kind: str, now_ms: int | None = None) -> None:
        at = int(time.time() * 1000) if now_ms is None else now_ms
        self.redis.hset(self.key(agent), mapping={"outcome_at": at, "outcome": kind, "talk": 0})

    def outcome_once(self, agent: str, kind: str, resolution: str, now_ms: int | None = None) -> bool:
        if self.redis.hget(self.key(agent), "resolved") == resolution:
            return False
        self.redis.hset(self.key(agent), "resolved", resolution)
        self.outcome(agent, kind, now_ms)
        return True


def checks_pass(
    redis: Any,
    slug: str,
    tasks: list[dict[str, Any]],
    github: Callable[[str], Any],
    now_ms: int | None = None,
) -> list[str]:
    marks: Progress = Progress(redis, slug)
    stamped: list[str] = []
    for task in tasks:
        url, owner = task.get("pr_url"), task.get("claimed_by")
        if task.get("state") != "pr" or not (url and owner):
            continue
        found = github(url)
        if found is None or not found.resolved:
            continue
        verdict = "red" if found.red else "green"
        if marks.outcome_once(owner, "checks resolved", f"{url} {found.pushed_at} {verdict}", now_ms):
            stamped.append(f"checks resolved {verdict} on {url}, an outcome for {owner}")
    return stamped
