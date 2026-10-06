import json
import uuid
from dataclasses import asdict, replace

from scripts.swarm import naming
from scripts.swarm.runtime import herdr_target
from scripts.swarm.store import MASTER, SwarmError

LOCK_MS = 600_000


def rename_swarm(store, slug, ledger, runtime, at):
    lock, token = store.key(slug, "tick-lock"), uuid.uuid4().hex
    if not store.redis.set(lock, token, nx=True, px=LOCK_MS):
        raise SwarmError(f"swarm {slug}: another tick or rename is running")
    try:
        config = store.ensure_code(slug)
        panes = {p["pane_id"]: p for p in runtime.herdr(["agent", "list"])["agents"]}
        actions, workspaces = [], set()
        for agent in store.agents(slug):
            pane = panes.get(agent.pane_id)
            if pane is None:
                raise SwarmError(f"no live pane for {agent.name}; run the swarm tick first")
            reserved = store.redis.get(store.key(slug, "rename-to", agent.name)) or ""
            _check_pane(store.names, agent, pane, reserved)
            new = _new_name(store, slug, agent, at)
            if pane.get("name") != herdr_target(new):
                runtime.herdr(["agent", "rename", agent.pane_id, herdr_target(new)])
                actions.append(f"renamed {agent.name} to {new}")
            store.names.alias(agent.name, new)
            _move_agent(store, slug, agent, new, at)
            for old in store.names.aliases(new):
                ledger.rename_agent(slug, old, new)
            workspaces.add(pane["workspace_id"])
        label = naming.space(config.repo, config.code, config.slug)
        for workspace in runtime.herdr(["workspace", "list"])["workspaces"]:
            if workspace["workspace_id"] not in workspaces and workspace.get("label") != f"swarm-{slug}":
                continue
            if workspace.get("label") != label:
                runtime.herdr(["workspace", "rename", workspace["workspace_id"], label])
                actions.append(f"renamed space {workspace['workspace_id']} to {label}")
        return actions
    finally:
        if store.redis.get(lock) == token:
            store.redis.delete(lock)


def _check_pane(names, agent, pane, reserved=""):
    from scripts.herdr_host import agent_name

    known = tuple(filter(None, (agent.name, names.resolve(agent.name), reserved, *names.aliases(agent.name))))
    accepted = {value for name in known for value in (name, name[:32], agent_name(name))}
    session = pane.get("agent_session") or {}
    if pane.get("name") not in accepted or (
        session.get("value") and agent.conversation_id and session["value"] != agent.conversation_id
    ):
        raise SwarmError(f"pane {agent.pane_id} belongs to another agent")


def _new_name(store, slug, agent, at):
    new = store.names.resolve(agent.name)
    if naming.parse(new):
        return new
    key = store.key(slug, "rename-to", agent.name)
    if reserved := store.redis.get(key):
        return reserved
    new = store.next_name(slug, agent.lane, at=agent.started_at or at)
    store.redis.set(key, new)
    store.names.note(new, session_id=agent.conversation_id)
    return new


def _move_agent(store, slug, agent, new, at):
    if agent.name != new:
        key = store.key(slug, "agents")
        claim = store.key(slug, "claim", agent.task)
        with store.redis.pipeline() as pipe:
            pipe.hset(key, new, json.dumps(asdict(replace(agent, name=new))))
            pipe.hdel(key, agent.name)
            if agent.task != MASTER and store.claimant(slug, agent.task) == agent.name:
                pipe.set(claim, new, keepttl=True)
            pipe.execute()
    if agent.seat:
        occupant = store.seats.occupant(agent.seat).occupant
        if occupant != new and store.names.resolve(occupant) == new:
            store.seats.occupy(agent.seat, new, at)
    for old in store.names.aliases(new):
        for kind in ("heartbeat", "wait"):
            key = store.key(slug, kind, old)
            if store.redis.exists(key):
                store.redis.renamenx(key, store.key(slug, kind, new))
                store.redis.delete(key)
