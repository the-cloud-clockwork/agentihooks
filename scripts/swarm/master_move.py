"""Move the master seat to another hive: after its hive's heartbeat goes quiet, or on the operator's master move."""

import json
from dataclasses import replace

from scripts.handoff import transfers
from scripts.hive import placement, registry
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import affinity
from scripts.swarm.store import MASTER, SwarmError

LOST = "hive-lost"
RECOVERY = """## Intent
Continue as the master of {slug}; the hive {hive} that ran {name} stopped its heartbeat {seconds} seconds ago.

## Done
unknown: {name} left no handoff document before its hive went quiet.

## Stopped at
unknown: the last step {name} took on {hive} was never reported.

## Decisions and promises
unknown: none recorded outside the ledger and the master inbox.

## Next
Re-read the ledger and the master inbox, then continue as master.

## Read first
- inbox:master

<!-- handoff complete -->
"""
ORDER = (
    "The operator moved the master to hive {to}; you run on {current}. Finish the step you are on, then hand off "
    "with the handoff skill: Handoff v2 carrying every pending decision and its asker, the messages you hold, what "
    "each claimed task needs, your audit plan and every operator override still in force. Run agentihooks swarm "
    "{slug} handoff <doc> --reason operator and stop. The runtime starts your successor on hive {to} in the master "
    "seat."
)


ACCOUNT_MOVED = (
    "The master now runs on {harness} account {account}; it moved from account {previous}. "
    "Open that account on claude.ai to reach it."
)


def fenced(hive: str) -> str:
    return f"{registry.key(hive)}:fenced"


def _key(store, slug):
    return store.key(slug, "master-move")


def pending(store, slug):
    raw = store.redis.get(_key(store, slug))
    return json.loads(raw) if raw else None


def quiet_for(store, config, master, now_ms) -> int:
    """Milliseconds the master's hive has been quiet past the grace, 0 while it beats or never beat."""
    record = registry.show(store.redis, master.hive) if master.hive else None
    beat = record["heartbeat_at"] if record else 0
    age = now_ms - beat
    return age if beat and age > config.master_move_grace * 1000 else 0


def target(store, config, master, now_ms, to=""):
    from scripts.hive.launch import _by_origin, loaded_hives

    hives = placement.for_swarm(loaded_hives(store), config.slug)
    hives = _by_origin([hive for hive in hives if to in ("", hive["id"])], config.repo)
    return placement.place(MASTER, master.harness, master.account, master.route_kind, config.repo, hives, now_ms)


def fail_over(slug, config, store, runtime, now_ms):
    master = affinity.live_master(store, slug)
    if master is None or master.name in runtime.live_names():
        return []
    age = quiet_for(store, config, master, now_ms)
    if not age:
        return []
    text = RECOVERY.format(slug=slug, name=master.name, hive=master.hive, seconds=age // 1000)
    store.put_handoff(slug, MASTER, text, seat=master.seat)
    transfers.record(store, slug, master, LOST, text, now_ms)
    generation = store.seats.occupy(master.seat, master.name, now_ms)
    fence = {"slug": slug, "generation": generation, "at": now_ms}
    store.redis.hset(fenced(master.hive), master.execution_id, json.dumps(fence))
    chosen = target(store, config, master, now_ms)
    to = chosen if isinstance(chosen, str) else ""
    found = {"from": master.hive, "to": to, "master": master.name, "state": "moving", "at": now_ms}
    store.redis.set(_key(store, slug), json.dumps(found))
    store.put_agent(slug, replace(master, state="finished"))
    return [f"moved master {master.name} off lost hive {master.hive}"]


def order(store, slug, to, now_ms):
    """Order the live master to hand off so its successor starts on hive to."""
    master = affinity.live_master(store, slug)
    if master is None:
        raise SwarmError("no live master to move")
    if to == master.hive:
        raise SwarmError(f"the master already runs on hive {to}")
    record = registry.show(store.redis, to)
    if record is None or not registry.live(record, now_ms):
        raise SwarmError(f"hive {to} is not live")
    chosen = target(store, store.config(slug), master, now_ms, to)
    if isinstance(chosen, placement.Wait):
        raise SwarmError(f"the master cannot move to hive {to}: {chosen.reason}")
    _withdraw(store, slug)
    text = ORDER.format(to=to, current=master.hive or "this machine", slug=slug)
    item = InboxStore(store.redis).send("operator", master.seat or master.name, text)
    found = {"from": master.hive, "to": to, "master": master.name, "item": item.id, "state": "ordered"}
    found.update(reason="", at=now_ms)
    store.redis.set(_key(store, slug), json.dumps(found))
    return found


def _withdraw(store, slug):
    found = pending(store, slug)
    if found and found.get("item"):
        InboxStore(store.redis).close(found["item"], "operator", "cancel", "the master move was replaced")


def pinned(store, slug, task, transfer):
    """The successor task pinned to the move's hive, only when its transfer follows the master the move named."""
    found = pending(store, slug)
    follows = found and transfer and found["master"] == transfer["predecessor"]
    return {**task, "hive": found["to"]} if follows and found["to"] else task


def placed(store, slug):
    found, inbox = pending(store, slug), InboxStore(store.redis)
    item = inbox.get(found["item"]) if found and found.get("item") else None
    if item and item.state not in CLOSED:
        inbox.close(item.id, item.address, "done", "the master moved to its hive")
    store.redis.delete(_key(store, slug))


def account_moved(slug, task, record, client=None):
    """Tell the operator in ledger chat when a master successor runs on another account than its predecessor."""
    from scripts.swarm.ledger_client import LedgerClient

    previous = ((task.get("handoff_envelope") or {}).get("launch") or {}).get("account") or ""
    if not (previous and record.account) or previous == record.account:
        return []
    text = ACCOUNT_MOVED.format(harness=record.harness, account=record.account, previous=previous)
    (client or LedgerClient()).say(slug, text)
    return [f"master {record.name} moved from account {previous} to {record.account}"]
