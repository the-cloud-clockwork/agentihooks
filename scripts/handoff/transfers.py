import json
import shlex
from uuid import uuid4

from scripts.swarm.store import SwarmError


def first_next(text: str) -> str:
    section = text.replace("\r\n", "\n").partition("## Next\n")[2].partition("\n## ")[0]
    return next((line.strip().lstrip("- ") for line in section.splitlines() if line.strip()), "")


def record(store, slug: str, agent, reason: str, text: str, at: int) -> dict:
    row = {
        "id": uuid4().hex,
        "reason": reason,
        "seat": agent.seat,
        "task": agent.task,
        "predecessor": agent.name,
        "successor": "",
        "at": at,
        "handoff": text,
        "next": first_next(text),
        "continuity": {"state": "pending"} if text else {"state": "unknown", "gap": "No handoff document"},
        "binding": {"state": "pending"},
    }
    store.redis.hset(store.key(slug, "transfers"), row["id"], json.dumps(row))
    return row


def list_transfers(store, slug: str) -> list[dict]:
    rows = [json.loads(v) for v in store.redis.hgetall(store.key(slug, "transfers")).values()]
    return sorted(rows, key=lambda row: (row["at"], row["id"]))


def last_handoff(store, slug: str, task: str) -> str:
    return next((r["handoff"] for r in reversed(list_transfers(store, slug)) if r["task"] == task and r["handoff"]), "")


def get(store, slug: str, transfer: str) -> dict:
    raw = store.redis.hget(store.key(slug, "transfers"), transfer)
    if not raw:
        raise SwarmError("No such handoff transfer")
    return json.loads(raw)


def _change(store, slug, transfer, change, seat=""):
    from redis.exceptions import WatchError

    key = store.key(slug, "transfers")
    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(key)
            if seat:
                pipe.watch(store.seats.key(seat))
            row = get(store, slug, transfer)
            change(row)
            pipe.multi()
            pipe.hset(key, transfer, json.dumps(row))
            pipe.execute()
            return row
        except WatchError as exc:
            raise SwarmError("The transfer changed; retry the command") from exc


def attach(store, slug: str, agent, at: int) -> dict | None:
    rows = [r for r in list_transfers(store, slug) if r["task"] == agent.task and not r["successor"]]
    if not rows:
        return None
    occupancy = store.seats.occupant(agent.seat)

    def change(row):
        row.update(successor=agent.name, seat=agent.seat, generation=occupancy.generation, attached_at=at)

    return _change(store, slug, rows[-1]["id"], change)


def fresh(store, slug: str, transfer: str, at: int) -> dict:
    def change(row):
        row.update(successor="", decision={"choice": "fresh", "at": at})
        row["binding"] = {"state": "pending"}

    return _change(store, slug, transfer, change)


def observe(store, slug: str, live: set[str], at: int) -> None:
    for row in list_transfers(store, slug):
        if not row["successor"] or row["binding"]["state"] == "live":
            continue
        occupancy = store.seats.occupant(row["seat"])
        if occupancy.occupant != row["successor"] or occupancy.generation != row["generation"]:
            continue
        state = "live" if row["successor"] in live else "absent"

        def change(current, state=state):
            current["binding"] = {"state": state, "at": at, "session": current["successor"]}

        _change(store, slug, row["id"], change)


def confirm(store, slug: str, transfer: str, agent, next_action: str, at: int) -> dict:
    def change(row):
        occupancy = store.seats.occupant(row["seat"])
        if (
            row["successor"] != agent.name
            or occupancy.occupant != agent.name
            or occupancy.generation != row["generation"]
        ):
            raise SwarmError("Only the current successor occupant can confirm this handoff")
        if not row["next"] or next_action.strip() != row["next"]:
            raise SwarmError("Confirm the first Next action exactly as written in the handoff")
        row["continuity"] = {"state": "confirmed", "at": at, "by": agent.name, "next": next_action.strip()}

    return _change(store, slug, transfer, change, agent.seat)


def priming(slug: str, transfer: dict | None) -> str:
    if not transfer or not transfer["handoff"]:
        return ""
    return (
        "Read this handoff before continuing. Confirm you read it and will take its first Next action by running "
        f"agentihooks swarm {slug} confirm-handoff {transfer['id']} --next "
        f"{shlex.quote(transfer['next'])}\n{transfer['handoff']}"
    )
