"""The one progress signal: each swarm agent's last outcome and the talk writes it made since, kept in Redis."""

import re
import time
from dataclasses import dataclass

from scripts.swarm.keyspace import ROOT

PREFIX = f"{ROOT}:swarm"
OUTCOME_COMMANDS = (
    ("pushed", re.compile(r"(?:^|[;&|(]\s*)git(?:\s+-C\s+\S+)?\s+push\b")),
    ("pull request opened", re.compile(r"(?:^|[;&|(]\s*)gh\s+pr\s+create\b")),
)


@dataclass(frozen=True)
class Mark:
    outcome_at: int = 0
    outcome: str = ""
    talk: int = 0


def outcome_of(command):
    return next((kind for kind, pattern in OUTCOME_COMMANDS if command and pattern.search(command)), "")


class Progress:
    def __init__(self, redis, slug):
        self.redis, self.slug = redis, slug

    def key(self, agent):
        return f"{PREFIX}:{self.slug}:progress:{agent}"

    def read(self, agent):
        raw = self.redis.hgetall(self.key(agent))
        return Mark(int(raw.get("outcome_at") or 0), raw.get("outcome", ""), int(raw.get("talk") or 0))

    def talk(self, agent):
        return int(self.redis.hincrby(self.key(agent), "talk"))

    def outcome(self, agent, kind, now_ms=None):
        at = int(time.time() * 1000) if now_ms is None else now_ms
        self.redis.hset(self.key(agent), mapping={"outcome_at": at, "outcome": kind, "talk": 0})

    def outcome_once(self, agent, kind, resolution, now_ms=None):
        if self.redis.hget(self.key(agent), "resolved") == resolution:
            return False
        self.redis.hset(self.key(agent), "resolved", resolution)
        self.outcome(agent, kind, now_ms)
        return True


def checks_pass(redis, slug, tasks, github, now_ms=None):
    marks, stamped = Progress(redis, slug), []
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
