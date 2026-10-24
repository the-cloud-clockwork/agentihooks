"""The member_swarms entry of the hub NATS signer record: hive invite lists each member it mints, hive scope the rest."""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.hive import auth, nats_grant, scope

if TYPE_CHECKING:
    from redis import Redis

FIELD = "member_swarms"


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


def backfill(redis: "Redis", bao: openbao_kv.OpenBao, path: str, named: Iterable[str]) -> str:
    added = assign(bao, path, scope.members(redis), swarms(redis, named), create=True)
    listed = ", ".join(added) if added else "no member"
    return f"OpenBao {openbao_kv.MOUNT}/{path}: wrote {FIELD} for {listed}"
