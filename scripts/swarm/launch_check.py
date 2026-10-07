"""The launch check: within a minute of launch every agent has joined the ledger in its seat, runs its assigned
profile, hooks, model and effort, sits on its package base role and carries a parsed swarm name."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hooks.context import profile_chain
from scripts.swarm import live_binding, naming
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import AgentRecord, RedisStore

DEADLINE_MS = 60 * 1000
STAMP = ".agentihooks-render.json"
SETTINGS = ("hooks", "model", "effort")
WORDS = {
    "joined": "joining the ledger and holding its seat",
    "profile": "its profile",
    "settings": "its hooks, model and effort",
    "overlay": "its role overlay on the package base role",
    "name": "its name",
}
REPORT_ONLY = frozenset({"overlay"})
OUTCOMES = {
    "relaunch": "It is being retired and relaunched once.",
    "spent": "Its one automatic relaunch is spent; operator action is required.",
    "report": "This is reported only; the master keeps running.",
}


@dataclass(frozen=True)
class Miss:
    agent: AgentRecord
    found: dict
    relaunch: bool


def begin(store: RedisStore, slug: str, agent: AgentRecord, at: int, relaunch: bool = True) -> None:
    entry = {"task": agent.task, "at": at, "relaunch": relaunch}
    store.redis.hset(store.key(slug, "launch-checks"), agent.name, json.dumps(entry))


def pending(store: RedisStore, slug: str) -> dict[str, dict]:
    return {name: json.loads(raw) for name, raw in store.redis.hgetall(store.key(slug, "launch-checks")).items()}


def forget(store: RedisStore, slug: str, name: str) -> None:
    store.redis.hdel(store.key(slug, "launch-checks"), name)


def chain(home) -> list[str]:
    try:
        return [str(name) for name in json.loads((Path(home) / STAMP).read_text())["chain"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def bundled(profile: str) -> bool:
    bundle = profile_chain.bundle_path(profile_chain.read_state())
    return bool(bundle and profile and (bundle / "profiles" / profile).is_dir())


def joined_at(agent: AgentRecord, doc: dict) -> int | None:
    return doc.get("_meta", {}).get("members", {}).get(agent.name, {}).get("joined_at")


def _joined(store, agent, doc, started_at):
    joined = joined_at(agent, doc)
    seated = bool(agent.seat) and store.seats.seat_of(agent.name) == agent.seat
    if joined is not None and joined - started_at <= DEADLINE_MS and seated:
        return {}
    actual = {
        "joined_after_ms": None if joined is None else joined - started_at,
        "seat": store.seats.seat_of(agent.name),
    }
    return {"joined": {"expected": {"joined_within_ms": DEADLINE_MS, "seat": agent.seat}, "actual": actual}}


def _process(agent, facts):
    differences = live_binding.compare(agent, facts)
    found = {}
    if "process" in differences or "profile" in differences:
        found["profile"] = {"expected": agent.profile, "actual": facts.get("profile")}
    if "process" in differences or any(key in differences for key in SETTINGS):
        expected = {**live_binding.assignment(agent), "hooks": True}
        found["settings"] = {
            "expected": {key: expected[key] for key in SETTINGS},
            "actual": {key: facts.get(key) for key in SETTINGS},
        }
    return found


def _overlay(agent, facts, on_bundle):
    found = facts.get("chain") or []
    ends = bool(found) and found[-1] == agent.profile
    if ends and (not on_bundle or any(name.startswith(profile_chain.PACKAGE_PREFIX) for name in found[:-1])):
        return {}
    base = f"{profile_chain.PACKAGE_PREFIX}<role>, " if on_bundle else ""
    return {"overlay": {"expected": f"{base}{agent.profile}", "actual": found}}


def _name(store, slug, agent):
    parsed = naming.parse(agent.name)
    code = store.config(slug).code
    if parsed and parsed.lane == agent.lane and parsed.code == code:
        return {}
    return {"name": {"expected": f"{naming.TYPES[agent.lane]}@{code}-<number>", "actual": agent.name}}


def misses(store: RedisStore, slug: str, agent: AgentRecord, facts: dict, doc: dict, on_bundle: bool) -> dict:
    return {
        **_joined(store, agent, doc, agent.started_at),
        **_process(agent, facts),
        **_overlay(agent, facts, on_bundle),
        **_name(store, slug, agent),
    }


def record(
    store: RedisStore, slug: str, agent: AgentRecord, found: dict, at: int, elapsed_ms: int, held: bool = False
) -> dict:
    report = {
        "agent": agent.name,
        "task": agent.task,
        "at": at,
        "elapsed_ms": elapsed_ms,
        "state": "failed" if found else "passed",
        "misses": found,
        "held": held,
    }
    store.redis.hset(store.key(slug, "launch-check-reports"), agent.task, json.dumps(report))
    return report


def report(store: RedisStore, slug: str, task: str) -> dict:
    raw = store.redis.hget(store.key(slug, "launch-check-reports"), task)
    return json.loads(raw) if raw else {}


def reports(store: RedisStore, slug: str) -> list[dict]:
    return [json.loads(raw) for raw in store.redis.hgetall(store.key(slug, "launch-check-reports")).values()]


def judged(store: RedisStore, slug: str) -> set[str]:
    reports = (json.loads(raw) for raw in store.redis.hgetall(store.key(slug, "launch-check-reports")).values())
    return set(pending(store, slug)) | {found["agent"] for found in reports if found.get("held")}


def relaunched(store: RedisStore, slug: str, task: str) -> bool:
    return bool(store.redis.sismember(store.key(slug, "launch-check-relaunched"), task))


def mark_relaunched(store: RedisStore, slug: str, task: str) -> None:
    store.redis.sadd(store.key(slug, "launch-check-relaunched"), task)


def clear_relaunched(store: RedisStore, slug: str, task: str) -> None:
    store.redis.srem(store.key(slug, "launch-check-relaunched"), task)


def told(found: dict, outcome: str) -> str:
    fields = ", ".join(WORDS[field] for field in found)
    return f"The master failed its launch check within a minute on {fields}. {OUTCOMES[outcome]}"


def findings(store: RedisStore, slug: str) -> list[Finding]:
    result = []
    for raw in store.redis.hgetall(store.key(slug, "launch-check-reports")).values():
        found = json.loads(raw)
        for field, values in found["misses"].items():
            if field in REPORT_ONLY:
                continue
            result.append(
                Finding(
                    "launch check",
                    f"{found['agent']}/{field}",
                    f"{found['agent']} failed its launch check on {field}",
                    (f"expected {values['expected']}; observed {values['actual']}",),
                    "a launch must pass within sixty seconds",
                    1,
                )
            )
    return result
