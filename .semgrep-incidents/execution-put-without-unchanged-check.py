from typing import Any, Protocol

from scripts.swarm import timing
from scripts.swarm.execution import _writes


class Put(Protocol):
    def put(self, slug: str, agent: Any) -> None: ...


class Unconditional(Put):
    # ruleid: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        with _writes(slug):
            timing.timed(self._put, slug, agent, managed)


class Prechecked(Put):
    # ok: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        if managed and self._unchanged(slug, agent):
            return
        with _writes(slug):
            timing.timed(self._put, slug, agent, managed)


class RecordPrechecked(Put):
    # ok: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        if managed:
            port = self.records(slug)
            if not isinstance(port, self.WholeDocumentRecords) and self._unchanged(slug, agent, port):
                return
        with _writes(slug):
            timing.timed(self._put, slug, agent, managed)


class CheckedInside(Put):
    # ruleid: execution-put-without-unchanged-check
    def put(self: Any, slug: str, agent: Any) -> None:
        managed = self.managed(slug, agent.name)
        with _writes(slug):
            if managed and self._unchanged(slug, agent):
                return
            timing.timed(self._put, slug, agent, managed)
