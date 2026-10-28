"""agentihooks hive remove: drain a hive, then delete it from Redis, herdr, OpenBao and antoncore."""

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.hive import auth, create, create_github, hive_state, machine, nats_grant, registry
from scripts.swarm.keyspace import ROOT

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
ROLL = (
    "Hive {hive}, which runs you, is stopping for a release and takes no new agents. Finish the step you are on, then "
    "hand off with the handoff skill even while you wait on checks, a merge or a reply, naming that wait under Next: "
    "run agentihooks swarm {slug} handoff <doc> --reason operator and stop. The runtime starts your successor on "
    "another hive."
)
MANIFESTS = (
    "k8s/argocd/apps/{release}.yaml",
    "k8s/manifests/{release}/",
    "k8s/values-overlays/{release}/",
)
MEMBER_ID = re.compile("[0-9a-f]{16}")
TREE = 'if .truncated then error("the tree listing is truncated") else .tree[] | select(.type == "blob") | .path end'


@dataclass(frozen=True)
class Plan:
    hive: str
    label: str
    bao_path: str
    repo: str = create.Options.repo
    base: str = create.Options.base
    wait_s: int = 3600
    signer: "nats_grant.Signer | None" = None
    dry_run: bool = False


@dataclass(frozen=True)
class Host:
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    known_hosts: Path = field(default_factory=lambda: Path.home() / ".ssh" / "known_hosts")
    home: Path = field(default_factory=registry.home)


def resolve(redis: "Redis", given: str) -> tuple[str, str]:
    if name := (auth.member(redis, given) or {}).get("name"):
        return _sole(redis, given, name), name
    if member := machine.named_member(redis, given):
        return _sole(redis, member, given), given
    if registry.show(redis, given) is not None or MEMBER_ID.fullmatch(given):
        raise auth.HiveError(f"hive {given} holds no member record to name it; pass --label")
    return given, given


def _sole(redis: "Redis", hive: str, name: str) -> str:
    for other, found in auth.members(redis).items():
        if other != hive and found["name"] == name:
            raise auth.HiveError(
                f"hive {hive} is named {name}, a name hive {other} also holds, so its herdr machine "
                "could be the other hive's; pass --label"
            )
    return hive


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

    if plan.dry_run:
        names = ", ".join(agent.name for _, agent in hosted(store, plan.hive)) or "no agents"
        return [f"would stop placement on {plan.hive} and wait up to {plan.wait_s} seconds for {names} to hand off"]
    if registry.show(store.redis, plan.hive) is not None:
        registry.update(store.redis, plan.hive, ["draining=yes"])
    inbox, ordered = InboxStore(store.redis), set()
    deadline = host.clock() + plan.wait_s
    while remaining := hosted(store, plan.hive):
        _order(inbox, ordered, remaining, ORDER, plan.hive)
        if host.clock() >= deadline:
            names = ", ".join(agent.name for _, agent in remaining)
            raise auth.HiveError(
                f"hive {plan.hive} takes no new agents, but {names} did not hand off within {plan.wait_s} seconds; "
                "run remove again to keep waiting"
            )
        host.sleep(POLL_S)
    handed = ", ".join(sorted(name for _, name in ordered)) or "none"
    return [f"drained {plan.hive}: {len(ordered)} agents handed off: {handed}"]


def _order(inbox, ordered: set, remaining: list[tuple[str, "AgentRecord"]], text: str, hive: str) -> None:
    for slug, agent in remaining:
        if (slug, agent.name) not in ordered:
            inbox.send(SENDER, agent.seat or agent.name, text.format(hive=hive, slug=slug))
            ordered.add((slug, agent.name))


def settle(store: "RedisStore", hive: str, wait_s: int, host: Host) -> str:
    from scripts.inbox.store import InboxStore

    if registry.show(store.redis, hive) is None:
        raise auth.HiveError(f"hive {hive} has no record to drain")
    registry.update(store.redis, hive, ["draining=yes"])
    inbox, ordered = InboxStore(store.redis), set()
    deadline = host.clock() + wait_s
    while remaining := hosted(store, hive):
        _order(inbox, ordered, remaining, ROLL, hive)
        if host.clock() >= deadline:
            names = ", ".join(agent.name for _, agent in remaining)
            return f"hive {hive} stops after {wait_s} seconds with {names} still running"
        host.sleep(POLL_S)
    return f"hive {hive} drained: no agents running, {len(ordered)} ordered to hand off"


def revoke(redis: "Redis", hive: str, signer: "nats_grant.Signer | None" = None, dry_run: bool = False) -> list[str]:
    if auth.member(redis, hive) is None:
        return [f"{hive} holds no member credential"]
    if dry_run:
        bus = [f"would revoke the NATS credential of {hive}"] if signer is not None else []
        return [*bus, f"would revoke member {hive} and its Redis user"]
    bus = [nats_grant.revoke(redis, signer, hive)] if signer is not None else []
    auth.revoke(redis, hive)
    return [*bus, f"revoked member {hive} and its Redis user"]


def unregister(redis: "Redis", plan: Plan, home: Path) -> list[str]:
    nodes = sorted(node for node, name in redis.hgetall(machine.MACHINES).items() if name == plan.label)
    state = home / create.STATE_FILE
    created = _created(state) == plan.label
    if plan.dry_run:
        held = redis.exists(*_keys(redis, plan.hive)) + _recorded(redis, plan.hive)
        lines = [f"would delete {held} registry records of {plan.hive}"]
    else:
        lines = forget(redis, plan.hive)
        if nodes:
            redis.hdel(machine.MACHINES, *nodes)
        if created:
            state.unlink()
    verb = "would release" if plan.dry_run else "released"
    lines += [f"{verb} the hive name {plan.label} of node {node}" for node in nodes]
    if created:
        lines.append(f"{'would delete' if plan.dry_run else 'deleted'} the hive create state of {plan.label}")
    return lines


def _created(state: Path) -> str | None:
    try:
        return json.loads(state.read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        return None


def _keys(redis: "Redis", hive: str) -> list[str]:
    return [registry.key(hive), *redis.scan_iter(match=f"{registry.key(hive)}:*")]


def _recorded(redis: "Redis", hive: str) -> bool:
    return hive_state.read(hive_state.records(redis, hive), hive) is not None


def forget(redis: "Redis", hive: str) -> list[str]:
    keys = _keys(redis, hive)
    prefix = f"{registry.key(hive)}:swarm:"
    histories = {
        f"{ROOT}:swarm:{source.removeprefix(prefix)}": redis.hgetall(source)
        for source in keys
        if source.startswith(prefix) and ":wait-history:" in source
    }
    with redis.pipeline() as pipe:
        for target, history in histories.items():
            pipe.hset(target, mapping=history)
        pipe.delete(*keys)
        deleted = pipe.execute()[-1]
    if _recorded(redis, hive):
        registry.forget(redis, hive)
        deleted += 1
    hive_state.indexed(redis, hive, False)
    return [f"deleted {deleted} registry records of {hive}"]


def unbank(bao: openbao_kv.OpenBao | None, path: str, dry_run: bool = False) -> list[str]:
    if dry_run:
        return [f"would delete OpenBao {openbao_kv.MOUNT}/{path}"]
    try:
        openbao_kv.delete(bao, path)
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    return [f"deleted OpenBao {openbao_kv.MOUNT}/{path}"]


def unmanifest(plan: Plan, run: Callable[..., subprocess.CompletedProcess]) -> list[str]:
    release = plan.label if plan.label.startswith(machine.NAME_PREFIX) else f"agentihooks-hive-{plan.label}"
    owned = [pattern.format(release=release) for pattern in MANIFESTS]
    tree = create_github.gh(run, ["api", f"repos/{plan.repo}/git/trees/{plan.base}?recursive=1", "--jq", TREE])
    paths = [
        path
        for path in tree.splitlines()
        if any(path == own or own.endswith("/") and path.startswith(own) for own in owned)
    ]
    if not paths:
        return [f"{plan.repo} {plan.base} holds no manifests of {release}"]
    listed = ", ".join(paths)
    if plan.dry_run:
        return [f"would open a pull request into {plan.repo} {plan.base} deleting {listed}"]
    message = f"Remove worker hive {plan.label}\n\nOpened by agentihooks hive remove."
    url = create_github.open_pr(run, plan.repo, plan.base, f"remove-{plan.label}", dict.fromkeys(paths), message)
    merging = "it merges once its checks pass" if create_github.merge(run, url) else "merge it once its checks pass"
    return [f"opened pull request {url} deleting {listed}; {merging}"]


STEPS = (
    ("drain", lambda store, bao, plan, host: drain(store, plan, host)),
    ("member", lambda store, bao, plan, host: revoke(store.redis, plan.hive, plan.signer, plan.dry_run)),
    ("herdr", lambda store, bao, plan, host: machine.forget(plan.label, host.known_hosts, host.run, plan.dry_run)),
    ("registry", lambda store, bao, plan, host: unregister(store.redis, plan, host.home)),
    ("manifests", lambda store, bao, plan, host: unmanifest(plan, host.run)),
    ("openbao", lambda store, bao, plan, host: unbank(bao, plan.bao_path, plan.dry_run)),
)


def remove(store: "RedisStore", bao: openbao_kv.OpenBao | None, plan: Plan, host: Host) -> list[str]:
    lines = [f"{title}: {line}" for title, step in STEPS for line in step(store, bao, plan, host)]
    if plan.dry_run:
        return [*lines, f"dry run of hive remove {plan.label}: nothing written"]
    return lines
