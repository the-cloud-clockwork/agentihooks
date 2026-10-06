"""Swarm agent names, `<type>@<code>-<number>`: the swarm code behind them and the registry that keeps both.

The only module that builds or parses an agent name. The code is six hex characters minted once per swarm; the number
counts each type on its own within the swarm and is never reused. Names the swarm gave before this scheme,
`<slug>-<lane>-<n>`, are still recognised so live agents keep working until they retire.
"""

import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

PREFIX = "agentihooks:names"
TYPES = {"master": "master", "eng": "engineer", "ci": "ci", "plan": "planner"}
LANES = {kind: lane for lane, kind in TYPES.items()}
NAME_RE = re.compile(r"(master|engineer|ci|planner)@([0-9a-f]{6})-(\d{4})")
LEGACY_RE = re.compile(r"(.+)-(eng|ci|master)-\d+")
CODE_RE = re.compile(r"[0-9a-f]{6}")
MINT_ATTEMPTS = 20


class NamingError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentName:
    kind: str
    code: str
    number: int

    @property
    def lane(self):
        return LANES[self.kind]

    def __str__(self):
        return build(self.kind, self.code, self.number)


def build(kind, code, number):
    if kind not in LANES or not CODE_RE.fullmatch(code) or not 0 < number < 10_000:
        raise NamingError(f"no agent name for type {kind!r}, code {code!r}, number {number!r}")
    return f"{kind}@{code}-{number:04d}"


def parse(name):
    found = NAME_RE.fullmatch(name or "")
    return AgentName(found.group(1), found.group(2), int(found.group(3))) if found else None


def legacy_slug(name):
    found = LEGACY_RE.fullmatch(name or "")
    return found.group(1) if found else ""


def lane_of(name):
    """The lane an agent name belongs to (eng, ci or master), '' for anything that is not an agent name."""
    parsed = parse(name)
    if parsed:
        return parsed.lane
    found = LEGACY_RE.fullmatch(name or "")
    return found.group(2) if found else ""


def space(repo, code):
    return f"{Path(repo).name}-{code}"


def plain(name):
    """The name for places that refuse an at sign: git branches, worktree folders and herdr agent names."""
    return name.replace("@", "-")


def _mint():
    return secrets.token_hex(3)


def resolve_name(name):
    from hooks._redis import get_redis

    redis = get_redis()
    return NameRegistry(redis).resolve(name) if redis is not None else name


def addresses(name):
    from hooks._redis import get_redis

    redis = get_redis()
    if redis is None:
        return [name]
    names = NameRegistry(redis)
    return [names.resolve(name), *names.aliases(name)]


class NameRegistry:
    """Global: each swarm code with its swarm, ledger and repo, and each name with its type, number, session and
    spawn and retire times."""

    def __init__(self, redis, mint=None):
        self.redis, self.mint = redis, mint or (lambda: _mint())

    @staticmethod
    def key(*parts):
        return ":".join((PREFIX, *parts))

    def resolve(self, name, reader=None):
        return (reader if reader is not None else self.redis).get(self.key("alias", name)) or name

    def alias(self, old, new):
        from redis.exceptions import WatchError

        new = self.resolve(new)
        if old == new:
            return
        if not self.entry(new):
            raise NamingError(f"no registered agent {new}")
        key = self.key("alias", old)
        for _ in range(4):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    held = pipe.get(key)
                    if held and held != new:
                        raise NamingError(f"alias {old} already belongs to {held}")
                    if pipe.exists(self.key("name", old)):
                        raise NamingError(f"registered name {old} cannot be an alias")
                    pipe.multi()
                    pipe.set(key, new)
                    pipe.sadd(self.key("aliases-of", new), old)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise NamingError(f"alias {old} changed meanwhile")

    def aliases(self, name, reader=None):
        reader = reader if reader is not None else self.redis
        return sorted(reader.smembers(self.key("aliases-of", self.resolve(name, reader))))

    def code_of(self, slug):
        return self.redis.hget(self.key("code-of"), slug) or ""

    def swarm(self, code):
        return json.loads(self.redis.hget(self.key("codes"), code) or "{}")

    def mint_code(self, slug, ledger, repo):
        """The swarm's code, minted and registered the first time; a code another swarm holds is minted again."""
        held = self.code_of(slug)
        if held:
            return held
        record = json.dumps({"swarm": slug, "ledger": ledger, "repo": repo})
        for _ in range(MINT_ATTEMPTS):
            code = self.mint()
            if not self.redis.hsetnx(self.key("codes"), code, record):
                continue
            if self.redis.hsetnx(self.key("code-of"), slug, code):
                return code
            self.redis.hdel(self.key("codes"), code)
            return self.code_of(slug)
        raise NamingError(f"no free swarm code after {MINT_ATTEMPTS} tries")

    def adopt(self, slug, code, ledger, repo):
        """Register a code the swarm record already carries, as after a restore into an emptied Redis."""
        self.redis.hsetnx(self.key("codes"), code, json.dumps({"swarm": slug, "ledger": ledger, "repo": repo}))
        self.redis.hsetnx(self.key("code-of"), slug, code)

    def release(self, slug):
        """Forget which code a removed swarm held; the code itself stays taken, so its names stay unique."""
        self.redis.hdel(self.key("code-of"), slug)

    def swarm_keys(self, slug):
        """The swarm's counters, its name list and each name's record, for a snapshot."""
        code = self.code_of(slug)
        if not code:
            return []
        listed = self.redis.lrange(self.key("names", code), 0, -1)
        keys = [self.key("seq", code), self.key("names", code)]
        for name in listed:
            keys += [self.key("name", name), self.key("aliases-of", name)]
            keys += [self.key("alias", old) for old in self.aliases(name)]
        return keys

    def next(self, slug, lane, at=0):
        code = self.code_of(slug)
        if not code:
            raise NamingError(f"swarm {slug} has no code yet")
        kind = TYPES[lane]
        name = build(kind, code, self.redis.hincrby(self.key("seq", code), kind, 1))
        fields = {"type": kind, "number": parse(name).number, "code": code, "swarm": slug}
        with self.redis.pipeline() as pipe:
            pipe.hset(self.key("name", name), mapping={**fields, "session_id": "", "spawned_at": at, "retired_at": 0})
            pipe.rpush(self.key("names", code), name)
            pipe.execute()
        return name

    def entry(self, name):
        name = self.resolve(name)
        raw = self.redis.hgetall(self.key("name", name))
        if not raw:
            return {}
        whole = ("number", "spawned_at", "retired_at")
        return {"name": name, **{k: int(v) if k in whole else v for k, v in raw.items()}}

    def note(self, name, **fields):
        name = self.resolve(name)
        if self.redis.exists(self.key("name", name)):
            self.redis.hset(self.key("name", name), mapping=fields)

    def retire(self, name, at):
        row = self.entry(name)
        if row and not row["retired_at"]:
            self.note(name, retired_at=at)

    def names(self, slug):
        code = self.code_of(slug)
        return [self.entry(name) for name in self.redis.lrange(self.key("names", code), 0, -1)] if code else []

    def slug_of(self, name):
        """The swarm an agent name belongs to, '' for anything else."""
        name = self.resolve(name)
        parsed = parse(name)
        if parsed:
            return self.swarm(parsed.code).get("swarm", "")
        return legacy_slug(name)

    def successor(self, name):
        """The agent of the same type that took over in the same swarm: the first one spawned after it that has not
        retired, '' when there is none yet."""
        name = self.resolve(name)
        parsed = parse(name)
        if not parsed:
            return ""
        later = self.redis.lrange(self.key("names", parsed.code), 0, -1)
        for other in later[later.index(name) + 1 :] if name in later else []:
            row = self.entry(other)
            if row.get("type") == parsed.kind and not row.get("retired_at"):
                return other
        return ""
