"""Read only loaders: the swarm store, the inbox and the saved prompts into the plain records the detectors take."""

import json
import re
from dataclasses import asdict
from pathlib import Path

from scripts.inbox.seats import of_swarm
from scripts.swarm.naming import NameRegistry
from scripts.swarm.store import MASTER, PREFIX

HANDOFF_START = "1. Handoff document: a previous agent ran out of context and left it. Continue from it:"
HANDOFF_END = "2. Swarm culture"
SEAT_RE = re.compile(r"^Your seat (\S+) carries", re.M)
TASK_RE = re.compile(r"^Your one task for this session is ([^:\s]+):", re.M)


def health_records(redis, slug):
    raw = redis.hgetall(":".join((PREFIX, slug, "findings")))
    return {finding_id: json.loads(record) for finding_id, record in raw.items()}


def inbox_items(inbox, slug):
    prefix = inbox.key("address", "")
    rows, names = [], NameRegistry(inbox.redis)
    for key in sorted(inbox.redis.scan_iter(match=prefix + "*")):
        for item in inbox.inbox(key[len(prefix) :]):
            if of_swarm(item.address, slug, names) or of_swarm(item.sender, slug, names):
                rows.append({**asdict(item), "history": inbox.history(item.id)})
    return rows


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


def handoffs(store, inbox, home, slug):
    sent = [{**row, "at": row["created_at"]} for row in inbox_items(inbox, slug)]
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
    for key in sorted(store.redis.scan_iter(match=waiting + "*")):
        task = key[len(waiting) :]
        seat = store.handoff_seat(slug, task)
        holder = store.seats.occupant(seat).occupant if seat else ""
        handoff = {"seat": seat, "task": task, "from": holder, "to": "", "at": 0, "document": store.handoff(slug, task)}
        found.append(_record(store, handoff, sent))
    return found
