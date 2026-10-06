"""Swarm runtime state in Redis: config, exclusive task claims with a lease, the agent registry and its seats."""

import json
import time
from dataclasses import asdict, dataclass, field, replace

from scripts.inbox.seats import SeatMemory, SeatRegistry, SwarmCulture, of_swarm
from scripts.inbox.store import InboxStore
from scripts.swarm.naming import NameRegistry

PREFIX = "agentihooks:swarm"
STATES = ("running", "paused", "stopping", "stopped", "drained")
DEFAULT_URL = "redis://127.0.0.1:6379/0"
MASTER = "master"
AUTONOMY = ("manual", "assist", "delegate", "full")
MANUAL, ASSIST, DELEGATE, FULL = AUTONOMY
CODEX_SHARE, CODEX_MIN_WEEK_LEFT = 30, 5


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
    codex_share: int | None = None
    codex_min_week_left: int | None = None
    snapshot_minutes: int | None = None
    code: str = ""
    max_plan: int = 1


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
    input_prompt: str = ""
    input_ticks: int = 0


class RedisStore:
    def __init__(self, redis):
        if redis is None:
            raise SwarmError("no Redis client; the swarm refuses to run without it")
        self.redis = redis
        self.seats = SeatRegistry(redis)
        self.memory = SeatMemory(redis)
        self.culture = SwarmCulture(redis)
        self.names = NameRegistry(redis)

    def key(self, slug, *parts):
        return ":".join((PREFIX, slug, *parts))

    def slugs(self):
        return sorted(self.redis.smembers(f"{PREFIX}:index"))

    def create(self, config):
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
            _whole(raw.get("codex_share")),
            _whole(raw.get("codex_min_week_left")),
            _whole(raw.get("snapshot_minutes")),
            raw.get("code", ""),
            int(raw.get("max_plan", 1)),
        )

    def update(self, slug, **changes):
        if changes.get("state", STATES[0]) not in STATES:
            raise SwarmError(f"state must be one of {STATES}")
        if changes.get("autonomy", DELEGATE) not in AUTONOMY:
            raise SwarmError(f"autonomy must be one of {AUTONOMY}")
        if not 0 <= changes.get("codex_share", 0) <= 100:
            raise SwarmError("codex share is a percent from 0 to 100")
        config = replace(self.config(slug), **changes)
        self.redis.hset(self.key(slug, "config"), mapping=_fields(config))
        return config

    def claim(self, slug, task, agent, lease_ms):
        return bool(self.redis.set(self.key(slug, "claim", task), agent, nx=True, px=lease_ms))

    def claimant(self, slug, task):
        return self.redis.get(self.key(slug, "claim", task))

    def refresh(self, slug, task, agent, lease_ms):
        return self._if_holder(self.key(slug, "claim", task), agent, lambda pipe, key: pipe.pexpire(key, lease_ms))

    def release(self, slug, task, agent):
        return self._if_holder(self.key(slug, "claim", task), agent, lambda pipe, key: pipe.delete(key))

    def _if_holder(self, key, agent, action):
        from redis.exceptions import WatchError

        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                if pipe.get(key) != agent:
                    return False
                pipe.multi()
                action(pipe, key)
                pipe.execute()
                return True
            except WatchError:
                return False

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

    def ensure_code(self, slug):
        config = self.config(slug)
        if config.code:
            self.names.adopt(slug, config.code, slug, config.repo)
            return config
        return self.update(slug, code=self.names.mint_code(slug, slug, config.repo))

    def next_name(self, slug, lane, at=0):
        self.ensure_code(slug)
        return self.names.next(slug, lane, at)

    def put_agent(self, slug, agent):
        from scripts.swarm.tick import agent_status

        previous = self.redis.hget(self.key(slug, "agents"), agent.name)
        if not previous or agent_status(AgentRecord(**json.loads(previous))) != agent_status(agent):
            self.redis.hset(self.key(slug, "state-since"), agent.name, int(time.time() * 1000))
        self.redis.hset(self.key(slug, "agents"), agent.name, json.dumps(asdict(agent)))

    def agents(self, slug):
        return [AgentRecord(**json.loads(v)) for _, v in sorted(self.redis.hgetall(self.key(slug, "agents")).items())]

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
        self.names.retire(name, ended)

    def count_spawn(self, slug, harness):
        self.redis.hincrby(self.key(slug, "spawns"), harness or "unknown", 1)

    def spawns(self, slug):
        return {harness: int(count) for harness, count in self.redis.hgetall(self.key(slug, "spawns")).items()}

    def count_claim(self, slug, task):
        return self.redis.hincrby(self.key(slug, "claims"), task, 1)

    def claims(self, slug, task):
        return int(self.redis.hget(self.key(slug, "claims"), task) or 0)

    def reset_claims(self, slug, task):
        self.redis.hdel(self.key(slug, "claims"), task)

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


def codex_split(config, environ):
    """(target share, minimum week left) in percent: the swarm setting, else the environment, else the default."""
    share = config.codex_share
    if share is None:
        share = int(environ.get("AGENTIHOOKS_SWARM_CODEX_SHARE") or CODEX_SHARE)
    floor = config.codex_min_week_left
    if floor is None:
        floor = int(environ.get("AGENTIHOOKS_SWARM_CODEX_MIN_WEEK_LEFT") or CODEX_MIN_WEEK_LEFT)
    return share, floor


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
