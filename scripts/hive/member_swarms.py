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
SYNC_DEFAULT = "agentihooks-swarm/agentihooks-swarm-nats-signer"
SYNC_TIMEOUT_S = 30
LATER = "hive join sees it at the next refresh"
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def signer_path(environ: Mapping[str, str]) -> str | None:
    path = environ.get(nats_grant.SIGNER_ENV)
    return path if path is not None and path.startswith(nats_grant.HUB_PATH) else None


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


def sync_target(environ: Mapping[str, str]) -> str | None:
    """The namespace/name of the ExternalSecret that mounts the hub signer record into hive join, or None."""
    if signer_path(environ) is None:
        return None
    return environ.get(SYNC_ENV, SYNC_DEFAULT) or None


def sync(target: str, run: Runner | None = None, clock: Callable[[], float] = time.time) -> str:
    """Force the signer ExternalSecret to resync so hive join reads a changed member listing at once."""
    namespace, _, name = target.partition("/")
    command = [
        "kubectl",
        "annotate",
        "externalsecret",
        name,
        "--namespace",
        namespace,
        f"force-sync={int(clock())}",
        "--overwrite",
    ]
    try:
        done = (run or subprocess.run)(command, capture_output=True, text=True, timeout=SYNC_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not force the sync of {target} ({exc}), so {LATER}"
    if done.returncode:
        reason = (done.stderr or "").strip() or f"exit {done.returncode}"
        return f"could not force the sync of {target} ({reason}), so {LATER}"
    return f"forced the sync of the signer record {target}, so hive join sees the change at once"


def drop(bao: openbao_kv.OpenBao, path: str, member: str, target: str | None, run: Runner | None = None) -> list[str]:
    """Unlist one member from the signer record and resync the mounted copy when it was listed."""
    if not unassign(bao, path, member):
        return [f"the hub signer record {path} lists no member swarms for {member}"]
    unlisted = f"unlisted {member} from the hub signer record {path}"
    return [unlisted] if target is None else [unlisted, sync(target, run)]


def backfill(redis: "Redis", bao: openbao_kv.OpenBao, path: str, named: Iterable[str]) -> str:
    added = assign(bao, path, scope.members(redis), swarms(redis, named), create=True)
    listed = ", ".join(added) if added else "no member"
    return f"OpenBao {openbao_kv.MOUNT}/{path}: wrote {FIELD} for {listed}"
