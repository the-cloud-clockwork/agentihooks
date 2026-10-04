"""Swarm runtime state in Redis: config, exclusive task claims with a lease, the agent registry."""

import json
from dataclasses import asdict, dataclass, replace

PREFIX = "agentihooks:swarm"
STATES = ("running", "paused", "stopping", "stopped", "drained")
DEFAULT_URL = "redis://127.0.0.1:6379/0"
MASTER = "master"


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


class RedisStore:
    def __init__(self, redis):
        if redis is None:
            raise SwarmError("no Redis client; the swarm refuses to run without it")
        self.redis = redis

    def key(self, slug, *parts):
        return ":".join((PREFIX, slug, *parts))

    def slugs(self):
        return sorted(self.redis.smembers(f"{PREFIX}:index"))

    def create(self, config):
        if not self.redis.hsetnx(self.key(config.slug, "config"), "slug", config.slug):
            raise SwarmError(f"swarm {config.slug} already exists")
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
        )

    def update(self, slug, **changes):
        if changes.get("state", STATES[0]) not in STATES:
            raise SwarmError(f"state must be one of {STATES}")
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

    def put_handoff(self, slug, task, text):
        self.redis.set(self.key(slug, "handoff", task), text)

    def handoff(self, slug, task):
        return self.redis.get(self.key(slug, "handoff", task)) or ""

    def clear_handoff(self, slug, task):
        self.redis.delete(self.key(slug, "handoff", task))

    def next_name(self, slug, lane):
        return f"{slug}-{lane}-{self.redis.incr(self.key(slug, 'seq', lane))}"

    def put_agent(self, slug, agent):
        self.redis.hset(self.key(slug, "agents"), agent.name, json.dumps(asdict(agent)))

    def agents(self, slug):
        return [AgentRecord(**json.loads(v)) for _, v in sorted(self.redis.hgetall(self.key(slug, "agents")).items())]

    def drop_agent(self, slug, name):
        self.redis.hdel(self.key(slug, "agents"), name)


def _fields(config):
    return {k: str(v) for k, v in asdict(config).items()}


def redis_url(environ):
    return environ.get("AGENTIHOOKS_SWARM_REDIS_URL") or DEFAULT_URL


def connect(environ=None):
    import os

    import redis

    import hooks.config  # noqa: F401  loads ~/.agentihooks/*.env, where REDIS_URL lives

    url = redis_url(os.environ if environ is None else environ)
    client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=10)
    try:
        client.ping()
    except redis.RedisError as exc:
        raise SwarmError(f"Redis is unreachable ({exc}); the swarm refuses to run without it") from exc
    return RedisStore(client)
