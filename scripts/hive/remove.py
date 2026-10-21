"""agentihooks hive remove: drain a hive, then delete it from Redis, herdr, OpenBao and antoncore."""

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.hive import auth, machine, registry

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import AgentRecord, RedisStore

ENDED = ("finished", "ended")
POLL_S = 15
SENDER = "operator"
ORDER = (
    "The operator is removing hive {hive}, which runs you. Finish the step you are on, then hand off with the "
    "handoff skill: run agentihooks swarm {slug} handoff <doc> --reason operator and stop. The runtime starts your "
    "successor on another hive."
)
MANIFESTS = (
    "k8s/argocd/apps/agentihooks-hive-{label}.yaml",
    "k8s/manifests/agentihooks-hive-{label}",
    "k8s/values-overlays/agentihooks-hive-{label}",
)


@dataclass(frozen=True)
class Plan:
    hive: str
    label: str
    bao_path: str
    antoncore: Path | None = None
    wait_s: int = 3600


@dataclass(frozen=True)
class Host:
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    known_hosts: Path = field(default_factory=lambda: Path.home() / ".ssh" / "known_hosts")


def label(redis: "Redis", hive: str) -> str:
    name = redis.hget(member_key(hive), "name")
    if not name:
        raise auth.HiveError(f"hive {hive} holds no member record to name it; pass --label")
    return name


def member_key(hive: str) -> str:
    return f"{auth.PREFIX}:member:{hive}"


def hosted(store: "RedisStore", hive: str) -> list[tuple[str, "AgentRecord"]]:
    return [
        (slug, agent)
        for slug in store.slugs()
        for agent in store.agents(slug)
        if agent.hive == hive and agent.state not in ENDED
    ]


def drain(store: "RedisStore", plan: Plan, host: Host) -> list[str]:
    from scripts.inbox.store import InboxStore

    registry.update(store.redis, plan.hive, ["draining=yes"])
    inbox, ordered = InboxStore(store.redis), set()
    deadline = host.clock() + plan.wait_s
    while remaining := hosted(store, plan.hive):
        for slug, agent in remaining:
            if (slug, agent.name) not in ordered:
                inbox.send(SENDER, agent.seat or agent.name, ORDER.format(hive=plan.hive, slug=slug))
                ordered.add((slug, agent.name))
        if host.clock() >= deadline:
            names = ", ".join(agent.name for _, agent in remaining)
            raise auth.HiveError(
                f"hive {plan.hive} takes no new agents, but {names} did not hand off within {plan.wait_s} seconds; "
                "run remove again to keep waiting"
            )
        host.sleep(POLL_S)
    return [f"drained {plan.hive}: {len(ordered)} agents handed off"]


def revoke(redis: "Redis", hive: str) -> list[str]:
    if not redis.exists(member_key(hive)):
        return [f"{hive} holds no member credential"]
    auth.revoke(redis, hive)
    return [f"revoked member {hive} and its Redis user"]


def forget(redis: "Redis", hive: str) -> list[str]:
    keys = [registry.key(hive), *redis.scan_iter(match=f"{registry.key(hive)}:*")]
    with redis.pipeline() as pipe:
        pipe.delete(*keys)
        pipe.srem(registry.INDEX, hive)
        deleted, _ = pipe.execute()
    return [f"deleted {deleted} registry records of {hive}"]


def unbank(bao: openbao_kv.OpenBao, path: str) -> list[str]:
    try:
        openbao_kv.delete(bao, path)
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    return [f"deleted OpenBao {openbao_kv.MOUNT}/{path}"]


def unmanifest(checkout: Path | None, name: str, run: Callable[..., subprocess.CompletedProcess]) -> list[str]:
    if checkout is None:
        return ["left the antoncore manifests: pass --antoncore with a checkout to remove them"]
    paths = [pattern.format(label=name) for pattern in MANIFESTS]
    argv = ["git", "-C", str(checkout), "rm", "-r", "-q", "--ignore-unmatch", "--", *paths]
    machine.run_checked(run, argv, None, machine.SSH_TIMEOUT_S)
    return [f"staged the removal of {', '.join(paths)} in {checkout}; commit it and open a pull request"]


def remove(store: "RedisStore", bao: openbao_kv.OpenBao, plan: Plan, host: Host) -> list[str]:
    return [
        *drain(store, plan, host),
        *revoke(store.redis, plan.hive),
        *machine.forget(plan.label, host.known_hosts, host.run),
        *forget(store.redis, plan.hive),
        *unbank(bao, plan.bao_path),
        *unmanifest(plan.antoncore, plan.label, host.run),
    ]
