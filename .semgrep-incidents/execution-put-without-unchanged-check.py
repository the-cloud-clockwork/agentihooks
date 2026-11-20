from typing import Any

from scripts.swarm import timing
from scripts.swarm.execution import _writes


class Unconditional:
    # ruleid: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        with _writes(slug):
            timing.timed(self._put, slug, agent, managed)


class Prechecked:
    # ok: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        if managed and self._unchanged(slug, agent):
            return
        with _writes(slug):
            timing.timed(self._put, slug, agent, managed)


class CheckedInside:
    # ruleid: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        with _writes(slug):
            if managed and self._unchanged(slug, agent):
                return
            timing.timed(self._put, slug, agent, managed)
