"""The declared Claude master account a master successor respawns on, and whether it keeps the master there."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from scripts.routing import master_account

if TYPE_CHECKING:
    from scripts.swarm.capacity import Account
    from scripts.swarm.store import AgentRecord, SwarmConfig

_Lanes = dict[str, dict[str, Any]]


def _lanes(config: SwarmConfig) -> _Lanes:
    """The config's lane entries by lane name."""
    return config.lanes  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def lane(config: SwarmConfig) -> dict[str, Any] | None:
    """The master lane entry of the config, absent when the swarm runs no master."""
    return _lanes(config).get("master")


def declared(environ: Mapping[str, str]) -> str:
    """The declared Claude master account a master successor respawns on, empty when none is declared or readable."""
    from redis.exceptions import RedisError

    try:
        found = master_account.load(environ).get("claude")
    except (OSError, RedisError):
        return ""
    return found.slug if found else ""


def with_declared(config: SwarmConfig, environ: Mapping[str, str]) -> SwarmConfig:
    """The config with the declared Claude master account on its master lane, where a master successor respawns."""
    master = lane(config)
    slug = declared(environ)
    if master is None or not slug:
        return config
    return replace(config, lanes={**_lanes(config), "master": {**master, "account": slug}})


def _read(observations: Iterable[Account], account: str) -> bool:
    """Whether the declared Claude master account has a quota reading, the only state a successor can leave it on."""
    return any(
        (row.harness, row.name) == ("claude", account) and (row.five_left is not None or row.week_left is not None)
        for row in observations
    )


def _front_door(slug: str, agent: AgentRecord) -> bool:
    """Whether the front door route forces this master's successor back onto its own account."""
    from scripts import front_door_route
    from scripts.swarm_v2 import masters

    return bool(front_door_route.check(agent.profile, masters.title(slug, agent.seat), []))


def pinned(master: dict[str, Any], observations: Iterable[Account], agents: Iterable[AgentRecord], slug: str) -> bool:
    """Whether a master successor stays on the declared account, so no worker seat is held for it elsewhere."""
    account: str = master.get("account") or ""
    if not account:
        return False
    if not _read(observations, account):
        return True
    return any(
        agent.lane == "master"
        and agent.state != "finished"
        and (agent.harness, agent.account) == ("claude", account)
        and _front_door(slug, agent)
        for agent in agents
    )
