"""Read only loaders: the swarm store, the inbox and the saved prompts into the plain records the detectors take."""

import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from scripts.inbox.seats import is_seat, of_swarm
from scripts.inbox.store import _item, _order
from scripts.swarm.naming import NameRegistry
from scripts.swarm.store import MASTER, PREFIX

BATCH = 1000
MINUTE_MS = 60_000
FINISHED = "finished"
OPERATOR = "operator"

HANDOFF_START = "1. Handoff document: a previous agent ran out of context and left it. Continue from it:"
HANDOFF_END = "2. Swarm culture"
SEAT_RE = re.compile(r"^Your seat (\S+) carries", re.M)
TASK_RE = re.compile(r"^Your one task for this session is ([^:\s]+):", re.M)


def health_records(redis, slug):
    raw = redis.hgetall(":".join((PREFIX, slug, "findings")))
    return {finding_id: json.loads(record) for finding_id, record in raw.items()}


def _batched(redis, keys, command):
    found = []
    for start in range(0, len(keys), BATCH):
        pipe = redis.pipeline()
        for key in keys[start : start + BATCH]:
            command(pipe, key)
        found += pipe.execute()
    return found


def inbox_items(inbox, slug):
    redis = inbox.redis
    keys = sorted(redis.scan_iter(match=inbox.key("address", "*"), count=BATCH))
    listed = _batched(redis, keys, lambda pipe, key: pipe.zrange(key, 0, -1))
    ids = [item_id for group in listed for item_id in group]
    raws = dict(zip(ids, _batched(redis, ids, lambda pipe, item_id: pipe.hgetall(inbox.key("item", item_id)))))
    grouped = [sorted((_item(raws[item_id], item_id) for item_id in group), key=_order) for group in listed]
    named = sorted({name for group in grouped for item in group for name in (item.address, item.sender)})
    slugs = NameRegistry(redis).slugs_of(named)
    names = SimpleNamespace(slug_of=lambda name: slugs[name])
    items = [
        item
        for group in grouped
        for item in group
        if of_swarm(item.address, slug, names) or of_swarm(item.sender, slug, names)
    ]
    histories = _batched(
        redis, [item.id for item in items], lambda pipe, item_id: pipe.lrange(inbox.key("history", item_id), 0, -1)
    )
    return [
        {**asdict(item), "history": [json.loads(entry) for entry in history]} for item, history in zip(items, histories)
    ]


def inbox_receivers(store, inbox, slug, items):
    """Each address the items reach and each agent that took delivery: scoped within the swarm, its Doctor and the
    operator, live while its agent runs."""
    from scripts.doctor.inbox import Receiver, reader
    from scripts.doctor.priming import doctor_slug
    from scripts.inbox.seats import _occupancy

    slugs, names = (slug, doctor_slug(slug)), NameRegistry(inbox.redis)
    agents = {a.name: a for s in slugs for a in store.agents(s) if a.state != FINISHED}
    addresses = sorted({item["address"] for item in items})
    owners = names.slugs_of(addresses)
    scoped = SimpleNamespace(slug_of=lambda name: owners[name])
    reachable = {
        address for address in addresses if address == OPERATOR or any(of_swarm(address, s, scoped) for s in slugs)
    }
    takers = {reader(item) for item in items if item["address"] in reachable}
    seats = sorted(address for address in reachable | takers if is_seat(address))
    seat_slugs = sorted({address.split("@", 1)[1] for address in seats})
    with inbox.redis.pipeline() as pipe:
        for address in seats:
            pipe.hgetall(inbox.seats.key(address))
        for address in seats:
            pipe.lrange(f"{inbox.seats.key(address)}:history", 0, -1)
        for s in seat_slugs:
            pipe.hgetall(store.key(s, "config"))
        rows = pipe.execute()
    occupancies = dict(zip(seats, map(_occupancy, rows[: len(seats)])))
    bound = {
        address: _bound_at(occupancies[address].generation, history)
        for address, history in zip(seats, rows[len(seats) :])
    }
    configs = dict(zip(seat_slugs, rows[2 * len(seats) :]))
    held = {address: occupancies[address].occupant if address in occupancies else address for address in addresses}
    resolved = names.resolve_many(sorted(set(held.values()) | {reader(item) for item in items}))
    departed = inbox.seats.exits(list(agents))
    found = {}
    for item in items:
        address, taker = item["address"], reader(item)
        if address != OPERATOR and not any(of_swarm(address, s, scoped) for s in slugs):
            found[address] = Receiver(scoped=False)
            continue
        for key, name in ((address, held[address]), (taker, taker)):
            if key and key not in found:
                agent = agents.get(resolved[name])
                found[key] = _inbox_receiver(key, agent, occupancies.get(key), departed, configs)
                if found[key].live and key in bound:
                    found[key] = replace(found[key], bound_at=bound[key])
    return found


def _bound_at(generation, history):
    entries = (json.loads(raw) for raw in history)
    return max((e["at"] for e in entries if e["generation"] == generation and "event" not in e), default=0)


def _inbox_receiver(address, agent, seat, departed, configs):
    from scripts.doctor.inbox import Receiver
    from scripts.inbox import exits

    if agent and (not is_seat(address) or departed[agent.name].get("generation") != seat.generation):
        return Receiver(
            live=True,
            quiet_ms=agent.idle_ticks * MINUTE_MS,
            occupant=agent.name,
            harness=agent.harness,
            task=agent.task,
        )
    if not is_seat(address):
        return Receiver()
    raw = configs[address.split("@", 1)[1]]
    config = (
        SimpleNamespace(
            state=raw["state"],
            max_eng=int(raw["max_eng"]),
            max_ci=int(raw["max_ci"]),
            max_plan=int(raw.get("max_plan", 1)),
        )
        if raw
        else None
    )
    waiting = config is None or (config.state != "stopped" and not exits.no_successor(address, config))
    return Receiver(seat_wait=waiting)


def _handoff(prompt):
    if HANDOFF_START not in prompt:
        return None
    body = prompt.split(HANDOFF_START, 1)[1]
    document = body.split("\n" + HANDOFF_END, 1)[0].strip("\n")
    seat, task = SEAT_RE.search(prompt), TASK_RE.search(prompt)
    return seat.group(1) if seat else "", task.group(1) if task else MASTER, document


def _record(store, handoff, sent):
    successor, at = handoff["to"], handoff["at"]
    return {
        **handoff,
        "recaps": store.memory.recaps(handoff["seat"]),
        "asked": [
            {k: m[k] for k in ("id", "address", "text", "at")}
            for m in sent
            if successor and m["sender"] == successor and m["at"] >= at
        ],
    }


def handoffs(store, inbox, home, slug, items=None):
    rows = inbox_items(inbox, slug) if items is None else items
    sent = [{**row, "at": row["created_at"]} for row in rows]
    found = []
    for path in sorted((Path(home) / slug / "prompts").glob("*.md")):
        parsed = _handoff(path.read_text())
        if parsed is None:
            continue
        seat, task, document = parsed
        history = store.seats.history(seat)
        mine = next((e for e in history if e["occupant"] == path.stem), None)
        if mine is None:
            continue
        before = next((e["occupant"] for e in history if e["generation"] == mine["generation"] - 1), "")
        handoff = {"seat": seat, "task": task, "from": before, "to": path.stem, "at": mine["at"], "document": document}
        found.append(_record(store, handoff, sent))
    waiting = ":".join((PREFIX, slug, "handoff", ""))
    for key in sorted(store.redis.scan_iter(match=waiting + "*", count=BATCH)):
        task = key[len(waiting) :]
        seat = store.handoff_seat(slug, task)
        author = (store.handoff_envelope(slug, task) or {}).get("agent", "")
        handoff = {"seat": seat, "task": task, "from": author, "to": "", "at": 0, "document": store.handoff(slug, task)}
        found.append(_record(store, handoff, sent))
    return found
