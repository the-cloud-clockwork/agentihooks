import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from scripts.hive import admission, placement_waits
from scripts.hive.placement import LOCAL_FULL, LOCAL_MACHINE
from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import capacity, freeze, timing
from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_client import LedgerGone

STALL_MS = 600_000
REF = "spawn-stall:"
LOCAL_CAP = "local cap"


def read(store, slug: str) -> dict:
    raw = store.redis.get(store.key(slug, "spawn-stall"))
    return json.loads(raw) if raw else {}


def save(store, slug: str, state: dict) -> None:
    timing.keep()
    store.redis.set(store.key(slug, "spawn-stall"), json.dumps(state))


def eligible(store, slug: str, ledger, at: int, runtime) -> bool:
    from scripts.swarm.tick import _ended

    config = store.config(slug)
    if config.state != "running":
        return False
    try:
        rows, ready = capacity.ready_work(slug, store, freeze.watched(slug, store, ledger, ledger.state(slug)))
    except LedgerGone:
        return False
    demand = {lane: len(tasks) for lane, tasks in ready.items()}
    agents = [agent for agent in store.agents(slug) if not _ended(agent, rows)]
    requirements = None
    if hasattr(runtime, "quota_requirements"):
        prepared = {lane: [capacity._prepared(store, slug, task) for task in tasks] for lane, tasks in ready.items()}
        requirements = runtime.quota_requirements(config, prepared)
    if reader := getattr(runtime, "quota_capacity", None):
        runtime.quota_previous(capacity.read(store, slug))
        capacity.feed_spent(runtime, store, at)
        decision = reader(config, agents, at / 1000, demand, requirements, refresh=False)
    else:
        inputs = capacity.live_inputs(slug, store, ledger, dict(os.environ), at)
        host = capacity.granted(capacity.host_room(config, inputs), inputs.previous, inputs.now_ms)
        config, _ = capacity.autoscaled(config, inputs, host)
        decision = capacity.calculate(
            config, inputs.observations, inputs.agents, inputs.demand, requirements, warned=inputs.warned
        )
        decision["host"] = host
    if (decision.get("host") or {}).get("room") == 0:
        return False
    return any(decision["placements"].values())


def launched(store, slug: str) -> int:
    return max(
        [0]
        + [row["at"] for row in store.launches(slug) if row["state"] == "started"]
        + [agent.started_at for agent in store.agents(slug) if agent.state in ("starting", "working")]
    )


def observe(store, slug: str, ledger, at: int, runtime) -> dict:
    state = read(store, slug)
    last = launched(store, slug)
    if not eligible(store, slug, ledger, at, runtime):
        state = {}
    elif "since" not in state or last > state["launch"]:
        state = {"since": at, "launch": last}
    state["at"] = at
    save(store, slug, state)
    return state


def _refused(reason: str) -> list[tuple[str, str]]:
    head, local, _ = reason.partition(LOCAL_FULL)
    _, room, held = head.partition(" has room: ")
    found = [(admission.category(part), part.split(" ", 1)[0]) for part in held.split("; ")] if room else []
    found = found or [(admission.category(head), "")]
    return found + [(LOCAL_CAP, LOCAL_MACHINE)] if local else found


def refusals(waits: dict) -> str:
    latest, counts = {}, {}
    for row in waits.values():
        for found in set(_refused(row["reason"])):
            latest[found] = max(latest.get(found, 0), row["started_at"])
            counts[found] = counts.get(found, 0) + 1
    shown = [
        f"{reason} on {host} ({counts[reason, host]})" if host else f"{reason} ({counts[reason, host]})"
        for reason, host in sorted(latest, key=lambda found: (-latest[found], found))
    ]
    return f"Admission refusals, latest first: {', '.join(shown)}" if shown else ""


def findings(store, slug: str) -> list[Finding]:
    state = read(store, slug)
    if "since" not in state or state["at"] - state["since"] < STALL_MS:
        return []
    failure = json.loads(store.redis.get(store.key(slug, "tick-failure")) or "{}")
    evidence = ("A lane has a free seat, quota and claimable work",)
    if refused := refusals(placement_waits.read(store, slug)):
        evidence += (refused,)
    if "at" in failure and failure["at"] >= state["since"]:
        evidence += (f"Last failed step {failure['step']}: {failure['error']}",)
    return [Finding("spawn stall", slug, "No agent has launched for ten minutes", evidence, "ten minutes", 1)]


def deliver(store, slug: str, ledger, at: int) -> None:
    found = findings(store, slug)
    if not found:
        return
    state = read(store, slug)
    inbox = InboxStore(store.redis)
    finding = found[0]
    text = f"Spawn stall on {slug}: {finding.summary}. " + "; ".join(finding.evidence)
    if "message" not in state:
        timing.keep()
        item = inbox.send("swarm", seat_address(slug, "master"), text, ref=f"{REF}{slug}:{state['since']}")
        state.update(message=item.id, sent_at=at)
        save(store, slug, state)
    item = inbox.get(state["message"])
    if item.state not in CLOSED and at - state["sent_at"] >= STALL_MS and not state.get("notified"):
        ledger.notify(slug, text)
        state["notified"] = True
        save(store, slug, state)


@contextmanager
def watch(store, slug: str, ledger, clock: Callable[[], int], runtime) -> Iterator[None]:
    previous = None

    def failed(step, error):
        nonlocal previous
        if error is previous:
            return
        previous = error
        failure = {"step": step, "error": f"{type(error).__name__}: {error}", "at": clock()}
        timing.keep()
        store.redis.set(store.key(slug, "tick-failure"), json.dumps(failure))

    token = timing.ON_FAILURE.set(failed)
    try:
        yield
    finally:
        timing.ON_FAILURE.reset(token)
        observe(store, slug, ledger, clock(), runtime)
        deliver(store, slug, ledger, clock())
