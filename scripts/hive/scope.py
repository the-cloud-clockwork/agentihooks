"""A hive member's Redis user writes only below its own hive key, so the inbox and the session readings of the agents
it runs live there; the controller finds them below the member each agent's execution record places it on."""

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis


def member(environ: Mapping[str, str] | None = None) -> str:
    """This process's hive member id while it talks Redis as that member, '' otherwise."""
    from scripts.swarm.store import redis_url

    env = os.environ if environ is None else environ
    own = env.get("AGENTIHOOKS_HIVE_ID", "")
    return own if own and redis_url(env) == env.get("AGENTIHOOKS_HIVE_REDIS_URL") else ""


def root(world: str) -> str:
    return registry.key(world) if world else ROOT


def host(redis: "Redis", slug: str, name: str) -> str:
    """The joined member that runs name in slug, '' for any other host; a member recognises only itself."""
    from scripts.hive.auth import PREFIX
    from scripts.swarm.store import RedisStore

    placed = next((agent.hive for agent in RedisStore(redis).agents(slug) if agent.name == name), "")
    own = member()
    if own:
        return own if placed == own else ""
    return placed if placed and redis.exists(f"{PREFIX}:member:{placed}") else ""


def readings(redis: "Redis", slug: str, name: str) -> str:
    """The key prefix of name's session readings in slug."""
    return f"{root(member() or host(redis, slug, name))}:swarm"
