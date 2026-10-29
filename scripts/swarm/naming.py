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
from functools import cached_property
from pathlib import Path

from scripts.events import state
from scripts.swarm.keyspace import ROOT
from scripts.swarm.name_state import KEEP, NameState, NamingError, state_key

PREFIX = f"{ROOT}:names"
TYPES = {"master": "master", "eng": "engineer", "ci": "ci", "plan": "planner", "dispatch": "dispatcher"}
LANES = {kind: lane for lane, kind in TYPES.items()}
OPERATOR = "operator"
_KINDS = "|".join(TYPES.values())
NAME_RE = re.compile(rf"({_KINDS})@([0-9a-f]{{6}})-(\d{{4}})")
LEGACY_RE = re.compile(r"(.+)-(eng|ci|master)-\d+")
CODE_RE = re.compile(r"[0-9a-f]{6}")
SWARM_RE = re.compile(r"swarm@([0-9a-f]{6})")
MINT_ATTEMPTS = 20
NAMES_PAGE = 500
_BASE = rf"(?:(?:{_KINDS})-[0-9a-f]{{6}}-\d{{4}}|session-[0-9a-f]{{8}})"
_REPO = r"[a-z0-9][a-z0-9._-]*"
PROOF_RE = re.compile(r"proof-[0-9a-f]{6}-[a-z0-9]+-\d+")
PATTERNS = {
    "agent": NAME_RE,
    "pane": re.compile(rf"(?:{_KINDS})-[0-9a-f]{{6}}-\d{{4}}"),
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
CODE_SWARM_KINDS = ("proof_slug", "demo_slug")
SWARM_NAME_RE = re.compile(r"[a-z]+(-[a-z]+)?-swarm")
SWARM_NAME_RULE = (
    "a swarm name is one or two lowercase words joined by a dash and ending in swarm, such as rig-grade-swarm or "
    "dating-swarm"
)
SUGGESTION_SKIP = frozenset(
    {"a", "an", "and", "by", "for", "from", "in", "into", "of", "on", "or", "plan", "swarm", "the", "to", "with"}
)
SUGGESTION_FALLBACK = ("new", "next", "work", "build")
SWARM_INIT = "agentihooks swarm init <name> --plan <file> --repo <dir>"


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
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def pane_label(title: str) -> str:
    return _clean(title) or "pane"


def seat_label(lane: str, index: int) -> str:
    if lane not in TYPES or type(index) is not int or index < 1:
        raise NamingError("a seat needs a known lane and a positive integer")
    return lane if lane in {"master", "dispatch"} and index == 1 else f"{lane}-{index}"


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
    return f"{_clean(swarm)}-{_clean(task)}"


def plan_slug(plan, date):
    stem = _clean(Path(plan).stem) or "plan"
    return f"{stem}-{_clean(date)}"


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


def is_code_named_swarm(slug):
    return any(PATTERNS[kind].fullmatch(slug) for kind in CODE_SWARM_KINDS)


def swarm_name_problem(name, taken=()):
    """Why NAME cannot name a new swarm; an empty string when it can."""
    if not name:
        return "a swarm needs a name"
    if not SWARM_NAME_RE.fullmatch(name):
        return f"{name} breaks the swarm name rule"
    return f"{name} is taken" if name in taken else ""


def swarm_name_suggestions(title, taken=(), count=2):
    """COUNT free swarm names built from the words of a plan title."""
    words = [word for word in re.findall(r"[a-z]+", str(title).lower()) if word not in SUGGESTION_SKIP]
    pairs = [f"{first}-{second}" for first, second in zip(words, words[1:])]
    stems = [*pairs[:1], *words, *pairs[1:], *SUGGESTION_FALLBACK]
    found = []
    for name in (f"{stem}-swarm" for stem in stems):
        if name not in found and not swarm_name_problem(name, taken):
            found.append(name)
    return found[:count]


def _mint():
    return secrets.token_hex(3)


def _record(slug, code, ledger, repo):
    return {"swarm": slug, "name": swarm_name(code), "ledger": ledger, "repo": repo}


def _unless_held(value):
    return lambda found: KEEP if found is not None else value


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

    @cached_property
    def names_state(self) -> NameState:
        return NameState(self.redis)

    @staticmethod
    def key(*parts):
        return ":".join((PREFIX, *parts))

    def watched(self, *parts):
        return self.names_state.watched(self.key(*parts))

    def _legacy_alias(self, name):
        return lambda client: client.get(self.key("alias", name))

    def _legacy_aliases_of(self, name):
        return lambda client: sorted(client.smembers(self.key("aliases-of", name)))

    def resolve(self, name, reader=None):
        try:
            return self.names_state.get("alias", name, self._legacy_alias(name), reader) or name
        except state.unavailable() as exc:
            logging.getLogger(__name__).warning("alias lookup failed for %s: %s", name, exc)
            return name

    def resolve_many(self, names):
        if not names:
            return {}
        try:
            found = self.names_state.get_many(
                "alias", names, lambda client, missing: client.mget([self.key("alias", name) for name in missing])
            )
        except state.unavailable() as exc:
            logging.getLogger(__name__).warning("alias lookup failed for %d names: %s", len(names), exc)
            return {name: name for name in names}
        return {name: alias or name for name, alias in zip(names, found)}

    def slugs_of(self, names):
        resolved = self.resolve_many(names)
        parsed = {name: parse(current) for name, current in resolved.items()}
        codes = sorted({found.code for found in parsed.values() if found})
        rows = self.names_state.get_many(
            "codes",
            codes,
            lambda client, missing: [
                json.loads(row) if row else None for row in client.hmget(self.key("codes"), missing)
            ],
        )
        swarms = {code: (row or {}).get("swarm", "") for code, row in zip(codes, rows)}
        return {name: swarms[found.code] if found else legacy_slug(resolved[name]) for name, found in parsed.items()}

    def alias(self, old, new):
        """Records the alias membership before the alias itself, so a reader that finds the alias finds its owner's
        list holding it."""
        new = self.resolve(new)
        if old == new:
            return
        if not self.entry(new):
            raise NamingError(f"no registered agent {new}")
        if self.redis.exists(self.key("name", old)):
            raise NamingError(f"registered name {old} cannot be an alias")

        def point(held):
            if held and held != new:
                raise NamingError(f"alias {old} already belongs to {held}")
            return KEEP if held == new else new

        point(self.names_state.get("alias", old, self._legacy_alias(old)))
        self.names_state.amend(
            "aliases-of",
            new,
            lambda held: KEEP if old in (held or []) else sorted({*(held or []), old}),
            self._legacy_aliases_of(new),
        )
        try:
            self.names_state.amend("alias", old, point, self._legacy_alias(old))
        except NamingError:
            self.names_state.amend(
                "aliases-of",
                new,
                lambda held: [kept for kept in held or [] if kept != old],
                self._legacy_aliases_of(new),
            )
            raise

    def aliases(self, name, reader=None):
        name = self.resolve(name, reader)
        return sorted(self.names_state.get("aliases-of", name, self._legacy_aliases_of(name), reader) or [])

    def _legacy_code_of(self, slug):
        return lambda client: client.hget(self.key("code-of"), slug)

    def _legacy_codes(self, code):
        return lambda client: json.loads(client.hget(self.key("codes"), code) or "null")

    def code_of(self, slug):
        return self.names_state.get("code-of", slug, self._legacy_code_of(slug)) or ""

    def swarm(self, code):
        return self.names_state.get("codes", code, self._legacy_codes(code)) or {}

    def _hold_code(self, code, record):
        return self.names_state.amend("codes", code, _unless_held(record), self._legacy_codes(code))

    def _hold_code_of(self, slug, code):
        return self.names_state.amend("code-of", slug, _unless_held(code), self._legacy_code_of(slug))

    def mint_code(self, slug, ledger, repo):
        """The swarm's code, minted and registered the first time; a code another swarm holds is minted again."""
        held = self.code_of(slug)
        if held:
            return held
        for _ in range(MINT_ATTEMPTS):
            code = self.mint()
            record = _record(slug, code, ledger, repo)
            if self._hold_code(code, record) is not record:
                continue
            owner = self._hold_code_of(slug, code)
            if owner == code:
                return code
            self.names_state.restore({state_key("codes", code): None})
            return owner
        raise NamingError(f"no free swarm code after {MINT_ATTEMPTS} tries")

    def adopt(self, slug, code, ledger, repo):
        """Register a code the swarm record already carries, as after a restore into an emptied Redis."""
        self._hold_code(code, _record(slug, code, ledger, repo))
        self._hold_code_of(slug, code)

    def swarm_slug(self, ref):
        """The ledger slug a swarm name `swarm@<code>` stands for while that swarm holds the code; ref otherwise."""
        try:
            code = swarm_code(ref)
            slug = self.swarm(code).get("swarm")
            return slug if slug and self.code_of(slug) == code else ref
        except state.unavailable() as exc:
            logging.getLogger(__name__).warning("swarm alias lookup failed for %s: %s", ref, exc)
            return ref

    def release(self, slug):
        """Forget which code a removed swarm held; the code itself stays taken, so its names stay unique."""
        self.names_state.amend("code-of", slug, lambda _: None, self._legacy_code_of(slug))

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
        names = self.redis.lrange(self.key("names", code), 0, -1) if code else []
        if not names:
            return []
        resolved = self.resolve_many(names)
        rows = []
        for start in range(0, len(names), NAMES_PAGE):
            with self.redis.pipeline(transaction=False) as pipe:
                for name in names[start : start + NAMES_PAGE]:
                    pipe.hgetall(self.key("name", resolved[name]))
                rows += pipe.execute()
        whole = ("number", "spawned_at", "retired_at")
        return [
            {"name": resolved[name], **{k: int(v) if k in whole else v for k, v in raw.items()}} if raw else {}
            for name, raw in zip(names, rows)
        ]

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


def registry():
    from hooks._redis import get_redis

    redis = get_redis()
    return NameRegistry(redis) if redis is not None else None


def registered_slug(name):
    """The swarm of a name from the registry, None without one; raises when its alias cannot be read, where slug_of
    would swallow the failure."""
    names = registry()
    if names is None:
        return None
    names.names_state.get("alias", name, names._legacy_alias(name))
    return names.slug_of(name)
