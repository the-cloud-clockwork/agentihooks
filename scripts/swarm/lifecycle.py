"""Per agent lifecycle controls for the ledger page: what each one changes, whether the page may offer it, and the
fenced, deduplicated path from a click to the controller. The page only displays offers; every request is checked
against the swarm store and the published task revision, and the controller fences the agent's incarnation and
generation again before any effect."""

import hashlib
import json
import os
import re
import traceback
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from scripts.swarm import commands, lease, live_binding
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError

VIEW, AGENT, SWARM = "terminal view", "one agent", "one swarm"
SWITCH = "AGENTIHOOKS_LIFECYCLE_DESTRUCTIVE"
COMMAND = "lifecycle"
OPERATION = re.compile(r"[A-Za-z0-9_-]{8,64}")
NAME = re.compile(r"[A-Za-z0-9@._-]{1,200}")
SEEN = ("incarnation", "generation", "task_revision")
SHOWN = 20
WRITE_ATTEMPTS = 5
WORKER_EXPIRY_MS = 300_000
PENDING_MS = 600_000
WORKER_HOLD_MS = 1_800_000
ISSUED = "issued"
UNSETTLED = frozenset({"issued", "accepted"})


@dataclass(frozen=True)
class Control:
    action: str
    label: str
    scope: str
    destructive: bool
    consequence: str


CONTROLS = (
    Control(
        "attach", "attach", VIEW, False, "Opens a terminal view of this agent; closing it leaves the agent running."
    ),
    Control(
        "pause", "pause", SWARM, False, "Pauses the whole swarm: no new agent starts, running agents keep working."
    ),
    Control(
        "drain", "drain", AGENT, False, "This agent checkpoints and takes no more work; other agents keep working."
    ),
    Control(
        "cancel", "cancel", AGENT, True, "This agent checkpoints and stops; its task stays claimed for a new attempt."
    ),
    Control(
        "force_stop",
        "force stop",
        AGENT,
        True,
        "Kills this agent now with no checkpoint; unsaved work in its pane is lost, its worktree stays.",
    ),
)
BY_ACTION = {control.action: control for control in CONTROLS}
AGENT_ACTIONS = tuple(control.action for control in CONTROLS if control.scope != SWARM)
SUPPORTED = {"local": frozenset({"force_stop"}), "kubernetes": frozenset({"attach", "drain", "cancel"})}
WORKER_KINDS = {"drain": "drain", "cancel": "cancel"}


class Refused(ValueError):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


def destructive_on(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(SWITCH, "on").strip().lower() != "off"


def incarnation(agent: AgentRecord) -> str:
    identity = json.dumps([agent.name, agent.execution_id, agent.generation, agent.started_at])
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def revisions(stamps: Mapping[str, Mapping]) -> dict[str, int]:
    found = {}
    for path, stamp in stamps.items():
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "state":
            found[parts[1]] = int(stamp.get("rev") or 0)
    return found


def published(store: RedisStore, slug: str) -> dict[str, int]:
    data = json.loads(store.redis.get(store.key(slug, "published")) or "{}")
    return data.get("status", {}).get("task_revisions") or {}


def disabled(agent: AgentRecord, action: str, pending: str, destructive: bool) -> str:
    control = BY_ACTION[action]
    if agent.state == "finished":
        return "the agent has finished"
    if action not in SUPPORTED.get(agent.runtime_backend, frozenset()):
        return f"a {agent.runtime_backend} agent has no {control.label}"
    if agent.runtime_backend != "local" and not agent.execution_id:
        return "the agent has no execution identity"
    if control.destructive and not destructive:
        return f"{control.label} is turned off; terminate still works"
    return "an earlier control on this agent is still unresolved" if pending else ""


def seen(agent: AgentRecord, task_revisions: Mapping[str, int]) -> dict:
    return {
        "incarnation": incarnation(agent),
        "generation": agent.generation,
        "task_revision": int(task_revisions.get(agent.task, 0)) if agent.task else 0,
    }


def offer(agent: AgentRecord, task_revisions: Mapping[str, int], pending: str, destructive: bool) -> dict:
    return {
        **seen(agent, task_revisions),
        "pending": pending,
        "disabled": {action: disabled(agent, action, pending, destructive) for action in AGENT_ACTIONS},
        "terminate": not destructive,
    }


def _pending(store: RedisStore, slug: str, reader, name: str, now: int) -> str:
    from scripts.swarm_v2.api.commands import CommandQueue

    op = reader.hget(_key(store, slug, "pending"), name)
    raw = op and reader.hget(_key(store, slug, "ops"), op)
    record = json.loads(raw) if raw else {}
    if record.get("state") == "queued":
        return op if now - record["at"] < PENDING_MS else ""
    if record.get("state") != ISSUED or now - record["at"] >= WORKER_HOLD_MS:
        return ""
    worker = CommandQueue(store, slug).outcome(record["execution_id"], record["result"]["worker_command"])
    return op if worker and worker["state"] in UNSETTLED else ""


def _key(store: RedisStore, slug: str, part: str) -> str:
    return store.key(slug, f"lifecycle-{part}")


def _agent(store: RedisStore, slug: str, name: str) -> AgentRecord | None:
    return next((agent for agent in store.agents(slug) if agent.name == name), None)


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def conflicts(store: RedisStore, slug: str) -> dict[str, int]:
    return {reason: int(count) for reason, count in store.redis.hgetall(_key(store, slug, "conflicts")).items()}


def _conflict(store: RedisStore, slug: str, reason: str, text: str) -> Refused:
    store.redis.hincrby(_key(store, slug, "conflicts"), reason, 1)
    return Refused(409, text)


def _parse(body: object) -> tuple[str, str, str, dict]:
    if not isinstance(body, Mapping):
        raise Refused(400, "a lifecycle control is a JSON object")
    action, name, op = body.get("control"), body.get("agent"), body.get("op")
    if action not in AGENT_ACTIONS:
        raise Refused(400, f"control must be one of {', '.join(AGENT_ACTIONS)}")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused(400, "a lifecycle control needs an exact agent name")
    if not isinstance(op, str) or not OPERATION.fullmatch(op):
        raise Refused(400, "a lifecycle control needs an operation id")
    shown = {key: body.get(key) for key in SEEN}
    if not isinstance(shown["incarnation"], str) or any(
        type(shown[key]) is not int or shown[key] < 0 for key in SEEN[1:]
    ):
        raise Refused(400, "a lifecycle control needs the incarnation, generation and task revision the page showed")
    return action, name, op, shown


def _admit(store: RedisStore, slug: str, pipe, body: tuple, task_revisions: Mapping[str, int], destructive: bool):
    action, name, op, shown = body
    fingerprint = _digest({"control": action, "agent": name, **shown})
    raw = pipe.hget(_key(store, slug, "ops"), op)
    if raw:
        record = json.loads(raw)
        if record["fingerprint"] != fingerprint:
            raise _conflict(store, slug, "reused", "the operation id was already sent with another control")
        return record, None
    agent = _agent(store, slug, name)
    if agent is None or agent.state == "finished" or seen(agent, task_revisions) != shown:
        raise _conflict(
            store,
            slug,
            "stale",
            "the page is stale: this agent was replaced, finished or moved task; wait for the next swarm view",
        )
    now = lease.now_ms(store)
    if _pending(store, slug, pipe, name, now):
        raise _conflict(store, slug, "pending", "an earlier control on this agent is still unresolved")
    reason = disabled(agent, action, "", destructive)
    if reason:
        raise Refused(422, reason)
    row = commands.prepare(store, slug, COMMAND, [action, name, op])
    record = {
        "op": op,
        "control": action,
        "agent": name,
        "execution_id": agent.execution_id,
        **shown,
        "fingerprint": fingerprint,
        "command": row["id"],
        "state": "queued",
        "error": "",
        "result": {},
        "at": now,
    }
    return record, row


def request(
    store: RedisStore,
    slug: str,
    body: object,
    *,
    operator: bool,
    environ: Mapping[str, str] = os.environ,
    task_revisions: Mapping[str, int] | None = None,
) -> dict:
    from redis.exceptions import WatchError

    parsed = _parse(body)
    control = BY_ACTION[parsed[0]]
    if control.destructive and not operator:
        raise Refused(403, f"{control.label} needs the operator")
    store.config(slug)
    for _ in range(WRITE_ATTEMPTS):
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(_key(store, slug, "ops"), _key(store, slug, "pending"))
                revisions = published(store, slug) if task_revisions is None else task_revisions
                record, row = _admit(store, slug, pipe, parsed, revisions, destructive_on(environ))
                if row is None:
                    return record
                pipe.multi()
                pipe.hset(_key(store, slug, "ops"), record["op"], json.dumps(record))
                pipe.rpush(_key(store, slug, "order"), record["op"])
                pipe.hset(_key(store, slug, "pending"), record["agent"], record["op"])
                commands.enqueue(pipe, store, slug, row)
                pipe.execute()
                return record
            except WatchError:
                continue
    raise Refused(503, "lifecycle controls kept changing; nothing was queued")


def _fenced(agent: AgentRecord | None, record: dict, destructive: bool, now: int) -> str:
    control = BY_ACTION[record["control"]]
    if now - record["at"] >= PENDING_MS:
        return "the control expired before the controller ran it"
    if control.destructive and not destructive:
        return f"{control.label} was turned off before the control ran"
    if agent is None:
        return "the agent is no longer in this swarm"
    if agent.state == "finished":
        return "the agent has finished"
    if (incarnation(agent), agent.generation) != (record["incarnation"], record["generation"]):
        return "the agent was replaced before the control ran"
    return ""


def _retire_managed(agent: AgentRecord) -> str:
    from scripts.swarm import runtime

    herdr = runtime.HerdrRuntime()
    return "" if herdr.retire(agent) else herdr.refusal(agent)["refusal"]


def _apply(store: RedisStore, slug: str, agent: AgentRecord, record: dict, run: Callable[[list[str]], str]):
    from scripts.swarm_v2.api.commands import CommandQueue
    from scripts.swarm_v2.auth_context import GrantRefused

    action = record["control"]
    if action == "attach":
        return run(["swarm", slug, "terminal", agent.execution_id]), {}
    if action in WORKER_KINDS:
        try:
            issued = CommandQueue(store, slug).issue(
                agent.execution_id, agent.generation, WORKER_KINDS[action], {}, record["op"], WORKER_EXPIRY_MS
            )
        except GrantRefused as error:
            return str(error), {}
        return "", {"worker_command": issued["command_id"]}
    if live_binding.managed(agent):
        return _retire_managed(agent), {}
    target = ["terminate-agent", agent.name, "--type", "any"]
    return run([*target, "--dry-run"]) or run(target), {}


def _settle(store: RedisStore, slug: str, record: dict, error: str, result: dict) -> None:
    from redis.exceptions import WatchError

    state = "failed" if error else ISSUED if "worker_command" in result else "applied"
    settled = {**record, "state": state, "error": error, "result": result}
    pending = _key(store, slug, "pending")
    for _ in range(WRITE_ATTEMPTS):
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(pending)
                held = pipe.hget(pending, record["agent"])
                pipe.multi()
                pipe.hset(_key(store, slug, "ops"), record["op"], json.dumps(settled))
                if held == record["op"] and state != ISSUED:
                    pipe.hdel(pending, record["agent"])
                pipe.execute()
                return
            except WatchError:
                continue
    store.redis.hset(_key(store, slug, "ops"), record["op"], json.dumps(settled))
    raise SwarmError("lifecycle controls kept changing; the outcome was recorded without its watch")


def execute(
    store: RedisStore, slug: str, row: dict, run: Callable[[list[str]], str], environ: Mapping[str, str] = os.environ
) -> str:
    action, name, op = row["argv"]
    raw = store.redis.hget(_key(store, slug, "ops"), op)
    if not raw:
        return "no such lifecycle operation"
    record = json.loads(raw)
    if record["state"] != "queued" or (record["control"], record["agent"]) != (action, name):
        return "the lifecycle operation does not match its command"
    agent = _agent(store, slug, name)
    error = _fenced(agent, record, destructive_on(environ), lease.now_ms(store))
    result = {}
    try:
        if not error:
            error, result = _apply(store, slug, agent, record, run)
    except Exception as exc:
        traceback.print_exc()
        error, result = f"{type(exc).__name__}: {exc}", {}
    _settle(store, slug, record, error, result)
    return error


def operations(store: RedisStore, slug: str, limit: int = SHOWN) -> list[dict]:
    from scripts.swarm_v2.api.commands import CommandQueue

    order = store.redis.lrange(_key(store, slug, "order"), -limit, -1)
    found = store.redis.hmget(_key(store, slug, "ops"), order) if order else []
    shown = []
    for raw in reversed([raw for raw in found if raw]):
        record = json.loads(raw)
        record.pop("fingerprint", None)
        command = record["result"].get("worker_command")
        if command:
            outcome = CommandQueue(store, slug).outcome(record["execution_id"], command)
            record["worker"] = {"state": outcome["state"], "outcome": outcome["outcome"]} if outcome else {}
        shown.append(record)
    return shown


def view(store: RedisStore, slug: str, environ: Mapping[str, str] = os.environ) -> dict:
    task_revisions, now = published(store, slug), lease.now_ms(store)
    destructive = destructive_on(environ)
    counts = conflicts(store, slug)
    return {
        "controls": [asdict(control) for control in CONTROLS],
        "destructive": destructive,
        "agents": {
            agent.name: offer(agent, task_revisions, _pending(store, slug, store.redis, agent.name, now), destructive)
            for agent in store.agents(slug)
        },
        "operations": operations(store, slug),
        "conflicts": counts,
        "ledger_control_conflicts_total": sum(counts.values()),
    }
