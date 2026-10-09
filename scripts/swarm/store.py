"""Swarm runtime state in Redis: config, exclusive task claims with a lease, the agent registry and its seats."""

import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING

from scripts.inbox.seats import SeatMemory, SeatRegistry, SwarmCulture, of_swarm
from scripts.inbox.store import InboxStore
from scripts.swarm import effort_range
from scripts.swarm.execution import ExecutionRegistry
from scripts.swarm.keyspace import ROOT
from scripts.swarm.naming import NameRegistry

if TYPE_CHECKING:
    from scripts.swarm_v2.runtime.operations import OperationJournal

PREFIX = f"{ROOT}:swarm"
STATES = ("running", "paused", "stopping", "stopped", "drained")
DEFAULT_URL = "redis://127.0.0.1:6379/0"
MASTER = "master"
AUTONOMY = ("manual", "assist", "delegate", "full")
MANUAL, ASSIST, DELEGATE, FULL = AUTONOMY
SCALING = ("auto", "manual")
AUTO_SCALING, MANUAL_SCALING = SCALING
DEFAULT_LOAD_HIGH, DEFAULT_LOAD_LOW = 1.5, 1.0
DEFAULT_MEMORY_PER_AGENT_MB = 700
MAX_LOAD = 10.0


class SwarmError(RuntimeError):
    pass


@dataclass(frozen=True)
class SwarmConfig:
    slug: str
    repo: str
    max_eng: int
    max_ci: int
    state: str = "running"
    compact_limit: int = 0
    template: str = ""
    lanes: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    autonomy: str = DELEGATE
    snapshot_minutes: int | None = None
    code: str = ""
    max_plan: int = 1
    gates: dict = field(default_factory=dict)
    effort_min: str = effort_range.DEFAULT[0]
    effort_max: str = effort_range.DEFAULT[1]
    overlays: dict = field(default_factory=dict)
    scaling: str = AUTO_SCALING
    load_high: float = DEFAULT_LOAD_HIGH
    load_low: float = DEFAULT_LOAD_LOW
    memory_per_agent_mb: int = DEFAULT_MEMORY_PER_AGENT_MB


def scaling_refusal(config):
    if config.scaling not in SCALING:
        return f"scaling must be one of {', '.join(SCALING)}"
    if (
        type(config.load_low) not in (int, float)
        or type(config.load_high) not in (int, float)
        or not 0 < config.load_low <= config.load_high <= MAX_LOAD
    ):
        return f"load low must be above 0 and at most load high, and load high at most {MAX_LOAD:g}"
    if type(config.memory_per_agent_mb) is not int or config.memory_per_agent_mb <= 0:
        return "memory per agent must be a whole number of MB above 0"
    return ""


@dataclass(frozen=True)
class AgentRecord:
    name: str
    lane: str
    task: str
    pane_id: str = ""
    harness: str = ""
    account: str = ""
    started_at: int = 0
    state: str = "working"
    idle_ticks: int = 0
    model: str = ""
    effort: str = ""
    seat: str = ""
    conversation_id: str = ""
    placement: str = ""
    profile: str = ""
    model_source: str = ""
    model_confidence: float | None = None
    profile_decision: dict = field(default_factory=dict)
    input_prompt: str = ""
    input_ticks: int = 0
    choice: str = ""
    launched_at: int = 0
    overlays: list = field(default_factory=list)
    execution_id: str = ""
    generation: int = 0
    runtime_backend: str = "local"
    runtime_target: dict = field(default_factory=dict)
    launch_timings: dict = field(default_factory=dict)
    hive: str = ""


class RedisStore:
    def __init__(self, redis):
        if redis is None:
            raise SwarmError("no Redis client; the swarm refuses to run without it")
        self.redis = redis
        self.seats = SeatRegistry(redis)
        self.memory = SeatMemory(redis)
        self.culture = SwarmCulture(redis)
        self.names = NameRegistry(redis)
        self.execution_registry = ExecutionRegistry(self)

    def key(self, slug, *parts):
        return ":".join((PREFIX, slug, *parts))

    @property
    def operation_journal(self) -> "OperationJournal":
        from scripts.swarm_v2.runtime.operations import OperationJournal

        return OperationJournal(self)

    def slugs(self):
        return sorted(self.redis.smembers(f"{PREFIX}:index"))

    def create(self, config):
        refused = effort_range.refusal((config.effort_min, config.effort_max), config.lanes) or scaling_refusal(config)
        if refused:
            raise SwarmError(refused)
        if not self.redis.hsetnx(self.key(config.slug, "config"), "slug", config.slug):
            raise SwarmError(f"swarm {config.slug} already exists")
        config = replace(config, code=self.names.mint_code(config.slug, config.slug, config.repo))
        self.redis.hset(self.key(config.slug, "config"), mapping=_fields(config))
        self.redis.sadd(f"{PREFIX}:index", config.slug)

    def config(self, slug):
        raw = self.redis.hgetall(self.key(slug, "config"))
        if not raw:
            raise SwarmError(f"no swarm {slug}")
        return SwarmConfig(
            raw["slug"],
            raw["repo"],
            int(raw["max_eng"]),
            int(raw["max_ci"]),
            raw["state"],
            int(raw.get("compact_limit", 0)),
            raw.get("template", ""),
            json.loads(raw.get("lanes") or "{}"),
            json.loads(raw.get("links") or "[]"),
            raw.get("autonomy") or DELEGATE,
            _whole(raw.get("snapshot_minutes")),
            raw.get("code", ""),
            int(raw.get("max_plan", 1)),
            json.loads(raw.get("gates") or "{}"),
            raw.get("effort_min") or effort_range.DEFAULT[0],
            raw.get("effort_max") or effort_range.DEFAULT[1],
            json.loads(raw.get("overlays") or "{}"),
            raw.get("scaling") or AUTO_SCALING,
            float(raw.get("load_high") or DEFAULT_LOAD_HIGH),
            float(raw.get("load_low") or DEFAULT_LOAD_LOW),
            int(raw.get("memory_per_agent_mb") or DEFAULT_MEMORY_PER_AGENT_MB),
        )

    def update(self, slug, **changes):
        if changes.get("state", STATES[0]) not in STATES:
            raise SwarmError(f"state must be one of {STATES}")
        if changes.get("autonomy", DELEGATE) not in AUTONOMY:
            raise SwarmError(f"autonomy must be one of {AUTONOMY}")
        previous = self.config(slug)
        config = replace(previous, **changes)
        if {"lanes", "effort_min", "effort_max"} & set(changes):
            refused = effort_range.refusal((config.effort_min, config.effort_max), config.lanes)
            if refused:
                raise SwarmError(refused)
            low, high = (effort_range.level(edge) for edge in (config.effort_min, config.effort_max))
            config = replace(config, effort_min=low, effort_max=high)
        if refused := scaling_refusal(config):
            raise SwarmError(refused)
        self.redis.hset(self.key(slug, "config"), mapping=_fields(config))
        return config

    def claim(self, slug: str, task: str, agent: str, lease_ms: int) -> bool:
        return self._if_holder(
            self.key(slug, "claim", task),
            None,
            lambda pipe, key: pipe.set(key, agent, nx=True, px=lease_ms),
            self._claim_guards(slug, task),
        )

    def claimant(self, slug, task):
        return self.redis.get(self.key(slug, "claim", task))

    def refresh(self, slug: str, task: str, agent: str, lease_ms: int) -> bool:
        return self._if_holder(
            self.key(slug, "claim", task),
            agent,
            lambda pipe, key: pipe.pexpire(key, lease_ms),
            self._claim_guards(slug, task),
        )

    def release(self, slug: str, task: str, agent: str) -> bool:
        return self._if_holder(
            self.key(slug, "claim", task),
            agent,
            lambda pipe, key: pipe.delete(key),
            self._claim_guards(slug, task),
        )

    def _if_holder(self, key, agent, action, guards=()):
        from redis.exceptions import WatchError

        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key, *guards)
                if any(pipe.exists(guard) for guard in guards) or pipe.get(key) != agent:
                    return False
                pipe.multi()
                action(pipe, key)
                return bool(pipe.execute()[0])
            except WatchError:
                return False

    def _claim_guards(self, slug, task):
        return self.key(slug, "task-authority", task), self.key(slug, "claim-journal", task)

    def put_handoff(self, slug, task, text, seat="", envelope=None):
        with self.redis.pipeline() as pipe:
            pipe.set(self.key(slug, "handoff", task), text)
            pipe.set(self.key(slug, "handoff-seat", task), seat)
            if envelope is None:
                pipe.delete(self.key(slug, "handoff-envelope", task))
            else:
                pipe.set(self.key(slug, "handoff-envelope", task), json.dumps(envelope))
            pipe.execute()

    def handoff_envelope(self, slug, task):
        raw = self.redis.get(self.key(slug, "handoff-envelope", task))
        return json.loads(raw) if raw else None

    def handoff(self, slug, task):
        return self.redis.get(self.key(slug, "handoff", task)) or ""

    def handoff_seat(self, slug, task):
        return self.redis.get(self.key(slug, "handoff-seat", task)) or ""

    def clear_handoff(self, slug, task):
        self.redis.delete(
            self.key(slug, "handoff", task),
            self.key(slug, "handoff-seat", task),
            self.key(slug, "handoff-envelope", task),
        )

    def put_reclaim(self, slug: str, name: str, verdict: dict) -> None:
        self.redis.hset(self.key(slug, "reclaims"), name, json.dumps(verdict))

    def reclaims(self, slug: str) -> dict[str, dict]:
        return {name: json.loads(raw) for name, raw in self.redis.hgetall(self.key(slug, "reclaims")).items()}

    def earlier_lives(self, slug: str, task: str) -> list[str]:
        rows = [json.loads(raw) for raw in self.redis.lrange(self.key(slug, "history"), 0, -1)]
        mine = [row for row in rows if row.get("task") == task and row.get("lane") != MASTER]
        return [row["name"] for row in sorted(mine, key=lambda row: row.get("ended_at") or 0, reverse=True)]

    def ensure_code(self, slug):
        config = self.config(slug)
        if config.code:
            self.names.adopt(slug, config.code, slug, config.repo)
            return config
        return self.update(slug, code=self.names.mint_code(slug, slug, config.repo))

    def next_name(self, slug, lane, at=0):
        self.ensure_code(slug)
        return self.names.next(slug, lane, at)

    def start_execution(self, slug: str, agent: AgentRecord, previous_execution_id: str = "") -> AgentRecord:
        return self.execution_registry.start(slug, agent, previous_execution_id)

    def execution(self, slug: str, execution_id: str) -> AgentRecord:
        return self.execution_registry.get(slug, execution_id)

    def executions(self, slug: str, seat: str) -> list[AgentRecord]:
        return [agent for agent in self.execution_registry.records(slug) if agent.seat == seat]

    def execution_occupants(self, slug: str) -> dict[str, AgentRecord]:
        return self.execution_registry.occupants(slug)

    def execution_identity_conflicts_total(self, slug: str) -> int:
        return int(self.redis.get(self.key(slug, "identity-conflicts")) or 0)

    def put_agent(self, slug, agent):
        self.execution_registry.put(slug, agent)

    def agents(self, slug):
        return self.execution_registry.agents(slug)

    def drop_agent(self, slug, name, at=None):
        ended = int(time.time() * 1000) if at is None else at
        previous = self.redis.hget(self.key(slug, "agents"), name)
        if previous:
            row = json.loads(previous)
            reason = "finished" if row["state"] == "finished" else "retired"
            if self.handoff(slug, row["task"]):
                reason = "handed off"
            self.redis.rpush(self.key(slug, "history"), json.dumps({**row, "ended_at": ended, "reason": reason}))
        self.redis.hdel(self.key(slug, "agents"), name)
        self.redis.hdel(self.key(slug, "state-since"), name)
        self.redis.hdel(self.key(slug, "retire-failures"), name)
        self.names.retire(name, ended)

    def count_spawn(self, slug, harness):
        self.redis.hincrby(self.key(slug, "spawns"), harness or "unknown", 1)

    def spawns(self, slug):
        return {harness: int(count) for harness, count in self.redis.hgetall(self.key(slug, "spawns")).items()}

    def count_claim(self, slug, task):
        self.redis.hsetnx(self.key(slug, "started-lives"), task, self._started_lives(slug, task))
        return self.redis.hincrby(self.key(slug, "started-lives"), task)

    def refund_claim(self, slug, task):
        self.redis.hsetnx(self.key(slug, "started-lives"), task, self._started_lives(slug, task))
        return self.redis.hincrby(self.key(slug, "started-lives"), task, -1)

    def claims(self, slug, task):
        counted = self.redis.hget(self.key(slug, "started-lives"), task)
        if counted is not None:
            return int(counted)
        return self._started_lives(slug, task)

    def reset_claims(self, slug, task):
        self.redis.hset(self.key(slug, "started-lives"), task, 0)
        self.redis.hdel(self.key(slug, "launch-failures"), task)

    def note_launch_failure(self, slug, task, reason):
        self.redis.hset(self.key(slug, "launch-failures"), task, reason)

    def launch_failure(self, slug, task):
        return self.redis.hget(self.key(slug, "launch-failures"), task) or ""

    def record_launch(self, slug: str, agent: AgentRecord, state: str, error: str = "") -> None:
        row = {"agent": agent.name, "task": agent.task, "at": agent.started_at, "state": state, "error": error}
        self.redis.hset(self.key(slug, "launches"), agent.name, json.dumps(row))

    def launches(self, slug: str) -> list[dict]:
        rows = {name: json.loads(raw) for name, raw in self.redis.hgetall(self.key(slug, "launches")).items()}
        history = [json.loads(raw) for raw in self.redis.lrange(self.key(slug, "history"), 0, -1)]
        for agent in [*history, *(asdict(a) for a in self.agents(slug))]:
            if agent.get("profile_decision", {}).get("validation", {}).get("state") == "validated":
                rows.setdefault(
                    agent["name"],
                    {
                        "agent": agent["name"],
                        "task": agent["task"],
                        "at": agent["started_at"],
                        "state": "started",
                        "error": "",
                    },
                )
        return list(rows.values())

    def _started_lives(self, slug, task):
        return sum(row["task"] == task and row["state"] == "started" for row in self.launches(slug))

    def put_restored(self, slug, outcomes):
        self.redis.set(self.key(slug, "restored"), json.dumps(outcomes))

    def restored(self, slug):
        return json.loads(self.redis.get(self.key(slug, "restored")) or "[]")

    def set_peer(self, slug, peer):
        self.redis.set(self.key(slug, "peer"), peer)

    def peer(self, slug):
        return self.redis.get(self.key(slug, "peer")) or ""

    def clear_peer(self, slug):
        self.redis.delete(self.key(slug, "peer"))

    def remove(self, slug):
        if not slug or set(slug) & set("*?[]\\"):
            raise SwarmError(f"refusing to remove swarm {slug!r}: an empty or pattern name would match other swarms")
        self.config(slug)
        if self.agents(slug):
            raise SwarmError(f"swarm {slug} still has agents; stop it with stop --now first")
        keys = list(self.redis.scan_iter(match=self.key(slug, "*")))
        if keys:
            self.redis.delete(*keys)
        self.redis.srem(f"{PREFIX}:index", slug)
        self.names.release(slug)

    def export(self, slug):
        """Everything the swarm holds in Redis: its own keys, its seats and their memory, its inbox addresses."""
        self.config(slug)
        own = [key for key in self.redis.scan_iter(match=self.key(slug, "*")) if key != self.key(slug, "tick-lock")]
        inbox, members = InboxStore(self.redis).keys_for(lambda address: of_swarm(address, slug, self.names))
        keys = (
            sorted(own) + self.seats.swarm_keys(slug) + [self.culture.key(slug)] + inbox + self.names.swarm_keys(slug)
        )
        return {"keys": _dump(self.redis, keys), "members": {f"{PREFIX}:index": [slug], **members}}

    def restore(self, slug, state):
        """Replace the swarm's own keys with the exported ones and write back its seats and inbox."""
        with self.redis.pipeline() as pipe:
            pipe.delete(*self.redis.scan_iter(match=self.key(slug, "*")), *state["keys"])
            for key, entry in state["keys"].items():
                _WRITE[entry["type"]](pipe, key, entry["value"])
                if entry["ttl_ms"] > 0:
                    pipe.pexpire(key, entry["ttl_ms"])
            for key, found in state["members"].items():
                pipe.sadd(key, *found)
            pipe.execute()
        self.ensure_code(slug)


def _fields(config):
    return {
        k: json.dumps(v) if isinstance(v, (dict, list)) else "" if v is None else str(v)
        for k, v in asdict(config).items()
    }


def _whole(raw):
    return int(raw) if raw else None


_READ = {
    "string": lambda redis, key: redis.get(key),
    "hash": lambda redis, key: redis.hgetall(key),
    "list": lambda redis, key: redis.lrange(key, 0, -1),
    "set": lambda redis, key: sorted(redis.smembers(key)),
    "zset": lambda redis, key: redis.zrange(key, 0, -1, withscores=True),
}
_WRITE = {
    "string": lambda pipe, key, value: pipe.set(key, value),
    "hash": lambda pipe, key, value: pipe.hset(key, mapping=value),
    "list": lambda pipe, key, value: pipe.rpush(key, *value),
    "set": lambda pipe, key, value: pipe.sadd(key, *value),
    "zset": lambda pipe, key, value: pipe.zadd(key, dict(value)),
}


def _dump(redis, keys):
    dumped = {}
    for key in keys:
        kind = redis.type(key)
        if kind in _READ:
            dumped[key] = {"type": kind, "value": _READ[kind](redis, key), "ttl_ms": max(redis.pttl(key), 0)}
    return dumped


def redis_url(environ):
    if environ.get("AGENTIHOOKS_DEPLOYMENT", "local") != "local" and environ.get("AGENTIHOOKS_HIVE_REDIS_URL"):
        return environ["AGENTIHOOKS_HIVE_REDIS_URL"]
    return environ.get("AGENTIHOOKS_SWARM_REDIS_URL") or DEFAULT_URL


def redis_client(environ=None):
    import os

    import redis

    import hooks.config  # noqa: F401  loads ~/.agentihooks/*.env, where REDIS_URL lives

    url = redis_url(os.environ if environ is None else environ)
    client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=10)
    client.ping()
    return client


def connect(environ=None):
    import redis

    try:
        return RedisStore(redis_client(environ))
    except redis.RedisError as exc:
        raise SwarmError(f"Redis is unreachable ({exc}); the swarm refuses to run without it") from exc
