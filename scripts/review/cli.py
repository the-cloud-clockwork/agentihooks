"""Agent command that posts review findings on a change request head as the calling swarm agent."""

import argparse
import os

from scripts.forge import remote
from scripts.forge.model import Forge
from scripts.gates.base import Who
from scripts.swarm import store as swarm_store
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError


def connect() -> RedisStore:
    return swarm_store.connect()  # pyright: ignore[reportUnknownMemberType]


def _forge() -> Forge:
    return remote.pick(remote.parse(remote.origin(".")), None, os.environ)


def _agent(found: RedisStore, slug: str) -> AgentRecord:
    name = found.names.resolve(Who.from_env().name)  # pyright: ignore[reportUnknownMemberType]
    agents = found.agents(slug)  # pyright: ignore[reportUnknownMemberType]
    agent = next((agent for agent in agents if agent.name == name), None)
    if agent is None:
        raise SwarmError(f"{name or 'this session'} is not an agent of swarm {slug}")
    return agent


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="agentihooks review")
    actions = found.add_subparsers(dest="action", required=True)
    post = actions.add_parser("post", help="post findings on the change request head through the forge")
    post.add_argument("url")
    post.add_argument("--slug", required=True)
    post.add_argument("--finding", action="append", default=[])
    return found


def main(argv: list[str]) -> int:
    raise NotImplementedError(argv)
