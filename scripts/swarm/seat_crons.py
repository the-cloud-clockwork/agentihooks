"""The standing master seat crons: one code owned list the agentihooks scheduler keeps aimed at the front door seat,
ensured when a swarm starts or its front door master joins and cancelled when the swarm stops or closes."""

from pathlib import Path
from typing import Any

from scripts.scheduler.core import Scheduler
from scripts.swarm.store import RedisStore, SwarmConfig

OWNER = "agentihooks master seat crons"
LOCK = "seat-crons-lock"
WAIT_S = 30


def scheduler() -> Scheduler:
    """This host's scheduler, started when it is not running."""
    from scripts.scheduler import commands

    return commands.connect()


def swarms() -> RedisStore:
    """The swarm store a ledger join reads the swarm and its master seats from."""
    from scripts.swarm import store

    return store.connect()  # pyright: ignore[reportUnknownMemberType]


def ensure(on: Scheduler, config: SwarmConfig, home: Path) -> list[dict[str, Any]]:
    """The seat crons the swarm wants, kept on the scheduler."""
    return []


def cancel(on: Scheduler, slug: str) -> list[str]:
    """Cancel every listed seat cron aimed at the swarm's front door seat."""
    return []


def on_start(store: RedisStore, slug: str) -> None:
    """Ensure the seat crons as a swarm starts."""


def on_stop(slug: str) -> None:
    """Cancel the seat crons as a swarm stops or closes."""


def on_join(slug: str, name: str) -> None:
    """Ensure the seat crons when the front door master joins."""
