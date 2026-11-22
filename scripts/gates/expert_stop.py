"""The Stop gate for an expert: a session of a running lineage does not end while its mission is open."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.gates.base import Call, Decision, Who

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore


class ExpertStop:
    name = "expert-stop"
    default_mode = "enforce"

    def __init__(self, connect: Callable[[], "RedisStore"] | None = None, now: Callable[[], int] | None = None) -> None:
        self._connect, self._now = connect, now

    def matches(self, call: Call) -> bool:
        raise NotImplementedError

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        raise NotImplementedError
