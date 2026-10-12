from __future__ import annotations

import fcntl
import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.records import _write

if TYPE_CHECKING:
    from scripts.swarm.health.findings import Finding
    from scripts.swarm.store import AgentRecord
    from scripts.swarm.tick import Runtime

HOME = None
PANE = "provider-error"
PANE_TTL_S = 24 * 3600
PAST_SPANS = 50
HELD = {"idle with claim": "idle during outage", "stale claim": "stale during outage"}


def home() -> Path:
    return HOME if HOME is not None else Path.home() / ".agentihooks" / "swarm"


def read(slug: str) -> dict:
    path = home() / slug / "outages.json"
    return json.loads(path.read_text()) if path.exists() else {}


@contextmanager
def _edit(slug: str):
    folder = home() / slug
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "outages.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read(slug)
        before = json.dumps(records)
        yield records
        if json.dumps(records) != before:
            _write(folder / "outages.json", records)


def observe(slug: str, kind: str, evidence: list[str], now_ms: int) -> None:
    with _edit(slug) as records:
        record = records.get(kind)
        if evidence:
            ongoing = record is not None and record["ended_at"] is None
            past = record.get("past") if ongoing else _history(record) if record else None
            fresh = {"kind": kind, "started_at": record["started_at"] if ongoing else now_ms, "evidence": evidence}
            records[kind] = {**fresh, "ended_at": None, **({"past": past} if past else {})}
        elif record and record["ended_at"] is None:
            record["ended_at"] = now_ms


def _history(record: dict) -> list:
    """Closed spans of a closed record's kind, its own included, which paused time still counts after a new outage
    of that kind opens."""
    return [*record.get("past", []), [record["started_at"], record["ended_at"]]][-PAST_SPANS:]


def open_records(slug: str) -> list[dict]:
    return [record for record in read(slug).values() if record["ended_at"] is None]


def lines(slug: str) -> list[str]:
    return [
        f"outage {record['kind']} since {record['started_at']}: {' ; '.join(record['evidence'])}"
        for record in open_records(slug)
    ]


def mark_pane(store: RedisStore, slug: str, name: str, error: str) -> None:
    store.redis.set(store.key(slug, PANE, name), error, ex=PANE_TTL_S)


def held(store: RedisStore, slug: str, name: str | None) -> str:
    """The evidence holding an agent: the swarm's open outages, else a provider error on its own pane."""
    evidence = [line for record in open_records(slug) for line in record["evidence"]]
    pane = store.redis.get(store.key(slug, PANE, name)) if name else None
    return "; ".join(evidence) or pane or ""


def paused_ms(slug: str, since_ms: int, now_ms: int) -> int:
    spans = sorted(
        (max(started_at, since_ms), now_ms if ended_at is None else min(ended_at, now_ms))
        for record in read(slug).values()
        for started_at, ended_at in [*record.get("past", []), [record["started_at"], record["ended_at"]]]
    )
    total, reach = 0, since_ms
    for start, end in spans:
        total += max(0, end - max(start, reach))
        reach = max(reach, end)
    return total


def hold_findings(store: RedisStore, slug: str, found: list[Finding], agents: list[AgentRecord]) -> list[Finding]:
    claimants = {agent.task: agent.name for agent in agents}
    return [_hold(store, slug, finding, claimants) for finding in found]


def _hold(store, slug, finding, claimants):
    if finding.kind not in HELD:
        return finding
    name = finding.subject if finding.kind == "idle with claim" else claimants.get(finding.subject)
    evidence = held(store, slug, name)
    if not evidence:
        return finding
    return replace(
        finding,
        kind=HELD[finding.kind],
        summary=f"{finding.summary}, held by an outage",
        evidence=(*finding.evidence, f"outage: {evidence}"),
    )


def show(slug: str, as_json: bool) -> bool:
    records = open_records(slug)
    if not records:
        return False
    if as_json:
        print(json.dumps({"outages": records}))
    else:
        print("\n".join(lines(slug)))
    return True


def _with_error(record, agent, pane, now_ms):
    from scripts.swarm import provider_probe

    if record is None:
        record = {"kind": "provider", "started_at": now_ms, "evidence": [], "ended_at": None}
    affected = record.setdefault("affected_agents", {})
    affected[agent.name] = {
        "harness": agent.harness,
        "account": agent.account,
        "model": agent.model,
        "error_id": pane.provider_error_id or pane.provider_error,
        "evidence": f"{agent.name} ({agent.harness}, account {agent.account or 'unknown'}): {pane.provider_error}",
    }
    target = provider_probe.Target(agent.harness, agent.account, agent.model)
    probes = record.setdefault("probes", {})
    route = provider_probe.key(target)
    previous = probes.get(route)
    if previous is None or previous["succeeded_at"] is not None:
        probes[route] = provider_probe.schedule(target, now_ms)
    return record


def _provider_record(record, observed, now_ms):
    seen = dict(record.get("observed_errors", {})) if record else {}
    episode = record if record and record["ended_at"] is None else None
    for agent, pane in observed:
        if not pane.provider_checked and not pane.provider_error:
            continue
        if not pane.provider_error:
            seen.pop(agent.name, None)
            continue
        error_id = pane.provider_error_id or pane.provider_error
        fresh = seen.get(agent.name) != error_id
        seen[agent.name] = error_id
        if fresh:
            episode = _with_error(episode, agent, pane, now_ms)
    if episode:
        if record and episode is not record:
            episode["past"] = _history(record)
        episode["observed_errors"] = seen
        if affected := episode.get("affected_agents"):
            episode["evidence"] = [actor["evidence"] for actor in affected.values()]
        return episode
    if record:
        record["observed_errors"] = seen
    return record


def providers(store: RedisStore, slug: str, runtime: Runtime, now_ms: int) -> None:
    from scripts.swarm import provider_probe

    observed = [(agent, runtime.observe(agent)) for agent in store.agents(slug) if agent.state != "finished"]
    with _edit(slug) as records:
        record = _provider_record(records.get("provider"), observed, now_ms)
        if record is None:
            return
        probes = record.get("probes", {})
        if record["ended_at"] is None and probes:
            directory = home() / slug / "provider-probes"
            if provider_probe.advance(probes, now_ms, lambda target: provider_probe.check(target, directory)):
                record["ended_at"] = max(state["succeeded_at"] for state in probes.values())
        records["provider"] = record


def ledger_health(slug: str, now_ms: int, probe: Callable[[], bool] | None = None) -> bool:
    from scripts.swarm_ledger import ledger_link

    try:
        healthy = probe() if probe is not None else bool(ledger_link.serving())
    except (SwarmError, OSError, SystemExit):
        healthy = False
    observe(slug, "ledger", [] if healthy else ["Ledger health probe failed"], now_ms)
    return healthy


def redis_state(evidence: list[str], now_ms: int) -> None:
    root = home()
    slugs = [path.name for path in root.iterdir() if path.is_dir()] if root.exists() else []
    for slug in slugs or ["_controller"]:
        observe(slug, "redis", evidence, now_ms)


def connect_tick(connector: Callable[[], RedisStore], clock: Callable[[], int]) -> RedisStore | None:
    from redis.exceptions import RedisError

    try:
        store = connector()
    except (SwarmError, RedisError) as exc:
        evidence = f"Redis connect check failed: {type(exc).__name__}"
        redis_state([evidence], clock())
        print(evidence)
        return None
    redis_state([], clock())
    return store


def guard_tick(function: Callable) -> Callable:
    @wraps(function)
    def guarded(store, slug, *args, **kwargs):
        from redis.exceptions import RedisError

        from scripts.swarm.cli import now_ms

        try:
            store.redis.ping()
        except RedisError as exc:
            evidence = f"Redis ping check failed: {type(exc).__name__}"
            observe(slug, "redis", [evidence], now_ms())
            return [evidence]
        try:
            actions = function(store, slug, *args, **kwargs)
        except RedisError as exc:
            evidence = f"Redis operation failed: {type(exc).__name__}"
            observe(slug, "redis", [evidence], now_ms())
            return [evidence]
        observe(slug, "redis", [], now_ms())
        return actions

    return guarded


def guard_command(function: Callable) -> Callable:
    @wraps(function)
    def guarded(store, args):
        from redis.exceptions import RedisError

        from scripts.swarm.cli import now_ms

        try:
            return function(store, args)
        except RedisError as exc:
            evidence = f"Redis operation failed: {type(exc).__name__}"
            redis_state([evidence], now_ms())
            print(evidence)
            return None

    return guarded
