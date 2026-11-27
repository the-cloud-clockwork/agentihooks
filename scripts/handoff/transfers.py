import json
import shlex
import time
from typing import Any
from uuid import uuid4

from scripts.swarm.store import AgentRecord, RedisStore, SwarmError

LAUNCH_FAILED = "Launch failed"


def now_ms() -> int:
    return int(time.time() * 1000)


def first_next(text: str) -> str:
    from scripts.handoff.check import section

    return next((line.strip().lstrip("- ") for line in section(text, "Next").splitlines() if line.strip()), "")


def record(store, slug: str, agent, reason: str, text: str, at: int, avoid_harness: str = "") -> dict:
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
    if avoid_harness:
        row["avoid_harness"] = avoid_harness
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


def attach(store, slug: str, agent) -> dict | None:
    rows = [r for r in list_transfers(store, slug) if r["task"] == agent.task and not r["successor"]]
    if not rows:
        return None
    occupancy = store.seats.occupant(agent.seat)

    def change(row):
        row.update(successor=agent.name, seat=agent.seat, generation=occupancy.generation, attached_at=now_ms())

    return _change(store, slug, rows[-1]["id"], change)


def fresh(store, slug: str, transfer: str, at: int) -> dict:
    def change(row):
        row.update(successor="", decision={"choice": "fresh", "at": at})
        row["binding"] = {"state": "pending"}

    return _change(store, slug, transfer, change)


def failed(store: RedisStore, slug: str, agent: AgentRecord, reason: str = LAUNCH_FAILED) -> None:
    for row in list_transfers(store, slug):
        if row["successor"] != agent.name or row["binding"]["state"] == "live":
            continue
        at = now_ms()

        def change(current: dict[str, Any], at: int = at) -> None:
            current["binding"] = {"state": "absent", "at": at, "session": agent.name, "reason": reason}

        _change(store, slug, row["id"], change)
        retry = {
            **row,
            "id": uuid4().hex,
            "at": at,
            "successor": "",
            "continuity": {"state": "pending"} if row["handoff"] else row["continuity"],
            "binding": {"state": "pending"},
            "retry_of": row["id"],
        }
        store.redis.hset(store.key(slug, "transfers"), retry["id"], json.dumps(retry))


def detach(store, slug: str, agent) -> None:
    """A spawn that waits on capacity hands its transfer back to the next spawn, recording no failed binding."""
    for row in list_transfers(store, slug):
        if row["successor"] != agent.name or row["binding"]["state"] == "live":
            continue

        def change(current):
            current["successor"] = ""

        _change(store, slug, row["id"], change)


def observe(store: RedisStore, slug: str, live: set[str]) -> None:
    from scripts.hive import reports

    agents = {agent.name: agent for agent in store.agents(slug)}
    unlaunched = {name for name, agent in agents.items() if agent.state in ("starting", "launching")}
    hives = reports.Reports(store.redis)
    for row in list_transfers(store, slug):
        if not row["successor"] or row["binding"]["state"] == "live" or row["successor"] in unlaunched:
            continue
        occupancy = store.seats.occupant(row["seat"])
        if occupancy.occupant != row["successor"] or occupancy.generation != row["generation"]:
            continue
        agent = agents.get(row["successor"])
        report = hives.read(reports.host(agent)) if agent is not None else None
        hosted = (
            agent is not None and report is not None and bool(hives.execution(agent)) and agent.name in report["names"]
        )
        state = "live" if row["successor"] in live or hosted else "absent"

        def change(current, state=state):
            current["binding"] = {"state": state, "at": now_ms(), "session": current["successor"]}

        _change(store, slug, row["id"], change)


def normalized_next(slug: str, agent: AgentRecord, action: str) -> str:
    if agent.lane == "master" and agent.harness == "codex":
        from scripts.profiles.codex_master import next_action
        from scripts.swarm import capabilities

        return next_action(action, slug, not capabilities.of_agent(agent).requires_foreground_inbox_wait)
    return action


def confirm(
    store: RedisStore, slug: str, transfer: str, agent: AgentRecord, next_action: str, at: int
) -> dict[str, Any]:
    def change(row: dict[str, Any]) -> None:
        occupancy = store.seats.occupant(row["seat"])
        if (
            row["successor"] != agent.name
            or occupancy.occupant != agent.name
            or occupancy.generation != row["generation"]
        ):
            raise SwarmError("Only the current successor occupant can confirm this handoff")
        expected = normalized_next(slug, agent, row["next"])
        if not expected or next_action.strip() != expected:
            raise SwarmError("Confirm the first Next action exactly as written in the handoff")
        row["continuity"] = {"state": "confirmed", "at": at, "by": agent.name, "next": next_action.strip()}

    return _change(store, slug, transfer, change, agent.seat)


def priming(slug: str, transfer: dict[str, Any] | None, *, seated: bool = True) -> str:
    if not transfer or not transfer["handoff"]:
        return ""
    instruction = (
        "After reading the handoff in your seat brief, confirm you read it and will take its first Next action by running "
        if seated
        else "Read this handoff before continuing. Confirm you read it and will take its first Next action by running "
    )
    text = (
        instruction
        + f"agentihooks swarm {slug} confirm-handoff {transfer['id']} --next "
        + shlex.quote(transfer["next"])
    )
    if not seated:
        text += f"\n{transfer['handoff']}"
    return text
