import json
import os
from collections.abc import Mapping

from scripts.gates import Who
from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger_route
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_v2 import masters

READY = "ledger-ready"


def _stamp(config, agent):
    return json.dumps(
        {
            "url": config.pending_api_url,
            "staged_at": config.ledger_staged_at,
            "execution_id": agent.execution_id,
            "generation": agent.generation,
        }
    )


def _fresh(config, agent):
    return bool(agent.execution_id and agent.launched_at >= config.ledger_staged_at and agent.state != "finished")


def acknowledge(store: RedisStore, slug: str, who: Who, environ: Mapping[str, str] = os.environ) -> bool:
    token = environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN")
    if not who.pinned or not token or not store.redis.hget(store.key(slug, "config"), "pending_api_url"):
        return False
    config = store.config(slug)
    agent = next((agent for agent in store.agents(slug) if agent.name == who.name), None)
    if agent is None or not _fresh(config, agent):
        return False
    stamp = _stamp(config, agent)
    if store.redis.hget(store.key(slug, READY), who.name) == stamp:
        return True
    try:
        ResourceClient(config.pending_api_url, {"X-Ledger-Token": token, "X-Ledger-Agent": who.name}).request(
            slug, "metadata"
        )
    except OSError:
        return False
    from redis.exceptions import WatchError

    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(store.key(slug, "config"), store.key(slug, "executions"))
            settings = pipe.hgetall(store.key(slug, "config"))
            current = json.loads(pipe.hget(store.key(slug, "executions"), agent.execution_id) or "null")
            if (
                settings.get("pending_api_url") != config.pending_api_url
                or int(settings.get("ledger_staged_at") or 0) != config.ledger_staged_at
                or current is None
                or current["state"] == "finished"
                or current["generation"] != agent.generation
            ):
                return False
            pipe.multi()
            pipe.hset(store.key(slug, READY), who.name, stamp)
            pipe.execute()
            return True
        except WatchError:
            return False


def readiness(store: RedisStore, slug: str) -> dict:
    config = store.config(slug)
    agents = [agent for agent in store.agents(slug) if agent.state != "finished"]
    receipts = store.redis.hgetall(store.key(slug, READY))
    missing = sorted(
        agent.name for agent in agents if not _fresh(config, agent) or receipts.get(agent.name) != _stamp(config, agent)
    )
    expected = {f"{masters.seat(index)}@{slug}" for index in range(1, masters.MasterSeats(store.redis).count(slug) + 1)}
    absent = sorted(expected - {agent.seat for agent in agents if agent.lane == "master"})
    return {
        "ready": bool(config.pending_api_url) and not missing and not absent,
        "missing": missing,
        "missing_masters": absent,
        "pending_url": config.pending_api_url,
        "active_url": config.api_url,
        "staged_at": config.ledger_staged_at,
    }


def settings(store: RedisStore, slug: str, changes: dict) -> SwarmConfig:
    config = store.config(slug)
    address = changes.get("api_url", config.api_url)
    if address == config.api_url:
        return store.update(slug, **changes)
    if set(changes) != {"api_url"}:
        raise SwarmError("the ledger endpoint must be activated separately")
    if not address or address != config.pending_api_url:
        raise SwarmError("the ledger destination must be staged and ready")
    _activate(store, slug, address)
    if (route := ledger_route.CURRENT.get()) and route[0] == slug:
        ledger_route.CURRENT.set((slug, address))
    return store.config(slug)


def _activation_keys(store: RedisStore, slug: str) -> dict[str, str]:
    return {
        **{store.key(slug, part): "hgetall" for part in ("config", "agents", "executions", "desired-routes", READY)},
        masters.MasterSeats(store.redis).key(slug): "hgetall",
        **{store.key(slug, part): "get" for part in ("tick-lock", "control-owner", "control-epoch")},
    }


def _activate(store: RedisStore, slug: str, address: str) -> None:
    from redis.exceptions import WatchError

    from scripts.swarm import commands
    from scripts.swarm_ledger import authority_context

    if Who.from_env().pinned:
        raise SwarmError("ledger activation requires controller authority")
    keys = _activation_keys(store, slug)
    with store.redis.pipeline() as batch:
        for key, reader in keys.items():
            getattr(batch, reader)(key)
        before = batch.execute()
    if not readiness(store, slug)["ready"]:
        raise SwarmError("the ledger successors are not ready")
    moved = None
    with authority_context.for_tick(store):
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(*keys)
                if [getattr(pipe, reader)(key) for key, reader in keys.items()] != before:
                    raise SwarmError("the ledger activation cohort changed")
                config = RedisStore(pipe).config(slug)
                if config.state != "paused":
                    raise SwarmError("the swarm must be paused before ledger activation")
                if pipe.exists(store.key(slug, "tick-lock")):
                    raise SwarmError("the swarm tick is still running")
                owner = commands.hive_id()
                raw = pipe.get(store.key(slug, "control-owner"))
                held = lease.Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
                if raw and (held is None or held.owner != owner):
                    raise SwarmError("the source controller ownership changed")
                epoch = int(pipe.get(store.key(slug, "control-epoch")) or 0)
                with ledger_route.scoped(slug, address):
                    target = lease.current(store, slug)
                    moved = lease.transfer(store, slug, owner, epoch, target.epoch if target else 0)
                pipe.multi()
                pipe.delete(store.key(slug, "control-owner"))
                pipe.hset(
                    store.key(slug, "config"),
                    mapping={"api_url": address, "pending_api_url": "", "ledger_staged_at": 0},
                )
                pipe.delete(store.key(slug, READY))
                pipe.execute()
            except WatchError as exc:
                if moved is not None:
                    with ledger_route.scoped(slug, address):
                        lease.release(store, slug, moved)
                raise SwarmError("the ledger activation changed before commit") from exc
