"""The member_swarms entry of the hub NATS signer record: hive invite lists each member it mints, hive scope the rest."""

import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from scripts import openbao_kv
from scripts.hive import auth, nats_grant, scope

if TYPE_CHECKING:
    from redis import Redis

FIELD = "member_swarms"
SYNC_ENV = "AGENTIHOOKS_NATS_SIGNER_SYNC"
SYNC_TARGET = "agentihooks-swarm/agentihooks-swarm-nats-signer"


def signer_path(environ: Mapping[str, str]) -> str | None:
    path = environ.get(nats_grant.SIGNER_ENV)
    return path if path is not None and path.startswith(nats_grant.HUB_PATH) else None


def unscoped(path: str) -> str:
    """The refusal for a hub signer record at path with no member_swarms field, naming the command that adds it."""
    return (
        f"the hub signer record {path} has no {FIELD} field, so the hub scopes no member and mints no bus "
        "credential; run agentihooks hive scope on the hub first"
    )


def sync_target(environ: Mapping[str, str]) -> str | None:
    if signer_path(environ) is None:
        return None
    return environ.get(SYNC_ENV, SYNC_TARGET) or None


def sync(target: str | None, run: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> list[str]:
    if target is None:
        return []
    namespace, _, name = target.partition("/")
    command = [
        "kubectl",
        "annotate",
        "externalsecret",
        name,
        "--namespace",
        namespace,
        f"force-sync={time.time_ns()}",
        "--overwrite",
    ]
    try:
        result = (run or subprocess.run)(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        reason = exc.strerror or str(exc)
    else:
        if result.returncode == 0:
            return [f"requested a sync of the signer record {target}"]
        reason = (result.stderr or "").strip() or f"exit {result.returncode}"
    return [f"could not force the sync of {target} ({reason}), so hive join sees it at the next refresh"]


def swarms(redis: "Redis", named: Iterable[str]) -> tuple[str, ...]:
    from scripts.swarm.store import RedisStore

    chosen = tuple(named) or tuple(RedisStore(redis).slugs())
    if not chosen:
        raise auth.HiveError("no swarm to list: pass --swarm or create a swarm first")
    return chosen


def listing(bao: openbao_kv.OpenBao, path: str) -> bool:
    try:
        return FIELD in openbao_kv.read(bao, path)[0]
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None


def assign(
    bao: openbao_kv.OpenBao, path: str, members: Iterable[str], chosen: tuple[str, ...], create: bool = False
) -> list[str]:
    added: list[str] = []

    def change(record: Mapping) -> dict:
        added.clear()
        if FIELD not in record and not create:
            return {}
        listed = dict(record.get(FIELD) or {})
        added.extend(member for member in members if member not in listed)
        if not added and FIELD in record:
            return {}
        merged = {**listed, **{member: list(chosen) for member in added}}
        nats_grant.member_swarms(merged)
        return {FIELD: merged}

    try:
        openbao_kv.update(bao, path, change)
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    return added


def extend(bao: openbao_kv.OpenBao, path: str, member: str, chosen: tuple[str, ...]) -> list[str]:
    """Add the chosen swarms member lacks to its listing, creating it, and return the swarms added."""
    added: list[str] = []

    def change(record: dict[str, Any]) -> dict[str, Any]:
        added.clear()
        listed: dict[str, Any] = dict(record.get(FIELD) or {})
        held = list(listed.get(member) or [])
        added.extend(swarm for swarm in chosen if swarm not in held)
        if not added:
            return {}
        merged = {**listed, member: [*held, *added]}
        nats_grant.member_swarms(merged)  # pyright: ignore[reportUnknownMemberType]
        return {FIELD: merged}

    try:
        openbao_kv.update(bao, path, change)  # pyright: ignore[reportUnknownMemberType]
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    return added


def unassign(bao: openbao_kv.OpenBao, path: str, member: str) -> bool:
    dropped: list[str] = []

    def change(record: dict[str, Any]) -> dict[str, Any]:
        dropped.clear()
        listed: dict[str, Any] = dict(record.get(FIELD) or {})
        if member not in listed:
            return {}
        del listed[member]
        dropped.append(member)
        return {FIELD: listed}

    try:
        openbao_kv.update(bao, path, change)  # pyright: ignore[reportUnknownMemberType]
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    return member in dropped


def backfill(redis: "Redis", bao: openbao_kv.OpenBao, path: str, named: Iterable[str]) -> str:
    added = assign(bao, path, scope.members(redis), swarms(redis, named), create=True)
    listed = ", ".join(added) if added else "no member"
    return f"OpenBao {openbao_kv.MOUNT}/{path}: wrote {FIELD} for {listed}"
