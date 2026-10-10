"""Swarm agent names, `<type>@<code>-<number>`: the swarm code behind them and the registry that keeps both.

The only module that builds or parses an agent name. The code is six hex characters minted once per swarm; the number
counts each type on its own within the swarm and is never reused. Names the swarm gave before this scheme,
`<slug>-<lane>-<n>`, are still recognised so live agents keep working until they retire.
"""

import json
import logging
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.swarm.keyspace import ROOT

PREFIX = f"{ROOT}:names"
TYPES = {"master": "master", "eng": "engineer", "ci": "ci", "plan": "planner", "dispatch": "dispatcher"}
LANES = {kind: lane for lane, kind in TYPES.items()}
OPERATOR = "operator"
NAME_RE = re.compile(r"(master|engineer|ci|planner|dispatcher)@([0-9a-f]{6})-(\d{4})")
LEGACY_RE = re.compile(r"(.+)-(eng|ci|master)-\d+")
CODE_RE = re.compile(r"[0-9a-f]{6}")
SWARM_RE = re.compile(r"swarm@([0-9a-f]{6})")
MINT_ATTEMPTS = 20
_BASE = r"(?:(?:master|engineer|ci|planner|dispatcher)-[0-9a-f]{6}-\d{4}|session-[0-9a-f]{8})"
_REPO = r"[a-z0-9][a-z0-9._-]*"
PROOF_RE = re.compile(r"proof-[0-9a-f]{6}-[a-z0-9]+-\d+")
PATTERNS = {
    "agent": NAME_RE,
    "pane": re.compile(r"(?:master|engineer|ci|planner|dispatcher)-[0-9a-f]{6}-\d{4}"),
    "space": re.compile(rf"{_REPO}-[0-9a-f]{{6}}|{PROOF_RE.pattern}"),
    "worktree": re.compile(rf"{_BASE}(?:-\d+)?"),
    "tmp": re.compile(rf"{_BASE}-tmp-\d+"),
    "scratch": re.compile(rf"{_REPO}/(?:[a-z][a-z0-9._-]*-[a-z0-9]+|{_BASE})"),
    "plan_slug": re.compile(r"[a-z0-9-]+-\d{4}-\d{2}-\d{2}"),
    "small_slug": re.compile(rf"small-{_BASE}"),
    "proof_slug": PROOF_RE,
    "demo_slug": re.compile(r"doctor-demo-\d{20}"),
}
SLUG_KINDS = ("plan_slug", "small_slug", "proof_slug", "demo_slug")


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


def swarm_name(code):
    """The swarm's own name, `swarm@<code>` as its agents' names; empty before the swarm holds a code. The code is
    minted once and never replaced, so the name never changes."""
    return f"swarm@{code}" if code else ""


def swarm_code(name):
    found = SWARM_RE.fullmatch(name)
    return found.group(1) if found else ""


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


def space(repo, code, slug=""):
    """The herdr workspace label: a proof swarm's own slug, else the git repository's name and the swarm code."""
    return slug if PROOF_RE.fullmatch(slug) else f"{repo_name(repo)}-{code}"


def plain(name):
    """The name for places that refuse an at sign: git branches, worktree folders and herdr agent names."""
    return name.replace("@", "-")


def _clean(text):
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-.")


def repo_name(path):
    """The git repository holding path, the same from its primary checkout or any worktree; the folder outside git."""
    try:
        found = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        found = ""
    common = Path(found)
    return _clean(common.parent.name if common.name == ".git" else Path(path).name) or "repo"


def session_base(environ):
    """The plain name of the swarm agent this session is, else session- and the first eight of its session id."""
    agent = parse(environ.get("AGENTIHOOKS_AGENT_NAME"))
    if agent:
        return plain(str(agent))
    session = re.sub(r"[^0-9a-f]", "", environ.get("CLAUDE_CODE_SESSION_ID", "").lower())
    if len(session) < 8:
        raise NamingError("no swarm agent name and no session id: names are built from the session that asks")
    return f"session-{session[:8]}"


def _first_free(names, taken):
    """The first of names not taken; names must hold one more candidate than taken, so one is always free."""
    return next(name for name in names if name not in taken)


def worktree(environ, taken=()):
    base, taken = session_base(environ), set(taken)
    return _first_free([base, *(f"{base}-{n}" for n in range(2, len(taken) + 2))], taken)


def tmp_worktree(environ, taken=()):
    base, taken = session_base(environ), set(taken)
    return _first_free([f"{base}-tmp-{n}" for n in range(1, len(taken) + 2)], taken)


def is_worktree(name, environ, tmp=False):
    base = re.escape(session_base(environ))
    return re.fullmatch(rf"{base}-tmp-\d+" if tmp else rf"{base}(?:-\d+)?", name) is not None


def scratch(environ, cwd):
    """`<repo>/<swarm>-<task>` for a swarm task, else `<repo>/<session base>`."""
    swarm, task = environ.get("AGENTIHOOKS_SWARM"), environ.get("AGENTIHOOKS_SWARM_TASK") or ""
    folder = scratch_folder(swarm, task) if swarm and _clean(task) else session_base(environ)
    return f"{repo_name(cwd)}/{folder}"


def scratch_folder(swarm, task):
    return f"{swarm}-{_clean(task)}"


def plan_slug(plan, date):
    stem = re.sub(r"[^a-z0-9]+", "-", Path(plan).stem.lower()).strip("-") or "plan"
    return f"{stem}-{date}"


def small_slug(environ):
    return f"small-{session_base(environ)}"


def proof_slug(environ, taken=()):
    """`proof-<swarm code>-<task>-<n>`, from the swarm agent asking and its task."""
    agent = parse(environ.get("AGENTIHOOKS_AGENT_NAME"))
    task = re.sub(r"[^a-z0-9]", "", environ.get("AGENTIHOOKS_SWARM_TASK", "").lower())
    if not agent or not task:
        raise NamingError("a proof swarm is named from its swarm agent and task: create it from a swarm task session")
    taken = set(taken)
    return _first_free([f"proof-{agent.code}-{task}-{n}" for n in range(1, len(taken) + 2)], taken)


def is_built_slug(slug):
    return any(PATTERNS[kind].fullmatch(slug or "") for kind in SLUG_KINDS)


def _mint():
    return secrets.token_hex(3)


def _record(slug, code, ledger, repo):
    return json.dumps({"swarm": slug, "name": swarm_name(code), "ledger": ledger, "repo": repo})


def resolve_name(name):
    from hooks._redis import get_redis

    redis = get_redis()
    return NameRegistry(redis).resolve(name) if redis is not None else name


def resolve_names(names):
    from hooks._redis import get_redis

    redis = get_redis()
    return NameRegistry(redis).resolve_many(names) if redis is not None else {name: name for name in names}


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
        from redis.exceptions import RedisError

        try:
            return (reader if reader is not None else self.redis).get(self.key("alias", name)) or name
        except RedisError as exc:
            logging.getLogger(__name__).warning("alias lookup failed for %s: %s", name, exc)
            return name

    def resolve_many(self, names):
        from redis.exceptions import RedisError

        if not names:
            return {}
        try:
            found = self.redis.mget([self.key("alias", name) for name in names])
        except RedisError as exc:
            logging.getLogger(__name__).warning("alias lookup failed for %d names: %s", len(names), exc)
            return {name: name for name in names}
        return {name: alias or name for name, alias in zip(names, found)}

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
        for _ in range(MINT_ATTEMPTS):
            code = self.mint()
            if not self.redis.hsetnx(self.key("codes"), code, _record(slug, code, ledger, repo)):
                continue
            if self.redis.hsetnx(self.key("code-of"), slug, code):
                return code
            self.redis.hdel(self.key("codes"), code)
            return self.code_of(slug)
        raise NamingError(f"no free swarm code after {MINT_ATTEMPTS} tries")

    def adopt(self, slug, code, ledger, repo):
        """Register a code the swarm record already carries, as after a restore into an emptied Redis."""
        self.redis.hsetnx(self.key("codes"), code, _record(slug, code, ledger, repo))
        self.redis.hsetnx(self.key("code-of"), slug, code)

    def swarm_slug(self, ref):
        """The ledger slug a swarm name `swarm@<code>` stands for while that swarm holds the code; ref otherwise."""
        from redis.exceptions import RedisError

        try:
            code = swarm_code(ref)
            slug = self.swarm(code).get("swarm")
            return slug if slug and self.code_of(slug) == code else ref
        except RedisError as exc:
            logging.getLogger(__name__).warning("swarm alias lookup failed for %s: %s", ref, exc)
            return ref

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
