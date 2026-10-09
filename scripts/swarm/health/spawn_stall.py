import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from scripts.inbox.seats import seat_address
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import capacity, timing
from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_client import LedgerGone

STALL_MS = 600_000
REF = "spawn-stall:"


def read(store, slug: str) -> dict:
    raw = store.redis.get(store.key(slug, "spawn-stall"))
    return json.loads(raw) if raw else {}


def save(store, slug: str, state: dict) -> None:
    store.redis.set(store.key(slug, "spawn-stall"), json.dumps(state))


def eligible(store, slug: str, ledger, at: int) -> bool:
    from scripts.swarm.tick import _ended

    config = store.config(slug)
    if config.state != "running":
        return False
    try:
        rows, ready = capacity.ready_work(slug, store, ledger.state(slug))
    except LedgerGone:
        return False
    agents = [agent for agent in store.agents(slug) if not _ended(agent, rows)]
    observations = capacity.accounts(dict(os.environ), at / 1000, refresh=False)
    decision = capacity.calculate(config, observations, agents, {lane: len(tasks) for lane, tasks in ready.items()})
    return any(decision["placements"].values())


def launched(store, slug: str) -> int:
    return max(
        [0]
        + [row["at"] for row in store.launches(slug) if row["state"] == "started"]
        + [agent.started_at for agent in store.agents(slug) if agent.state in ("starting", "working")]
    )


def observe(store, slug: str, ledger, at: int) -> dict:
    state = read(store, slug)
    last = launched(store, slug)
    if not eligible(store, slug, ledger, at):
        state = {}
    elif "since" not in state or last > state["launch"]:
        state = {"since": at, "launch": last}
    state["at"] = at
    save(store, slug, state)
    return state


def findings(store, slug: str) -> list[Finding]:
    state = read(store, slug)
    if "since" not in state or state["at"] - state["since"] < STALL_MS:
        return []
    failure = json.loads(store.redis.get(store.key(slug, "tick-failure")) or "{}")
    evidence = ("A lane has a free seat, quota and claimable work",)
    if failure:
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
        item = inbox.send("swarm", seat_address(slug, "master"), text, ref=f"{REF}{slug}:{state['since']}")
        state.update(message=item.id, sent_at=at)
        save(store, slug, state)
    item = inbox.get(state["message"])
    if item.state not in CLOSED and at - state["sent_at"] >= STALL_MS and not state.get("notified"):
        ledger.notify(slug, text)
        state["notified"] = True
        save(store, slug, state)


@contextmanager
def watch(store, slug: str, ledger, clock: Callable[[], int]) -> Iterator[None]:
    previous = None

    def failed(step, error):
        nonlocal previous
        if error is previous:
            return
        previous = error
        failure = {"step": step, "error": f"{type(error).__name__}: {error}"}
        store.redis.set(store.key(slug, "tick-failure"), json.dumps(failure))

    token = timing.ON_FAILURE.set(failed)
    try:
        yield
    finally:
        timing.ON_FAILURE.reset(token)
        observe(store, slug, ledger, clock())
        deliver(store, slug, ledger, clock())
