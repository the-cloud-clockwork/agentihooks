"""Runtime port over the runtime router for agent operations and herdr for the pane list."""

from collections.abc import Callable, Mapping
from pathlib import Path

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, SpawnRequest, Status
from scripts.tick.deadline import Deadline

__all__ = ["RouterRuntime"]


def _live(deadline: Deadline) -> None:
    if deadline.expired:
        raise TimeoutError("tool deadline expired")


def _herdr(args: list[str]) -> dict[str, object]:
    from scripts.swarm.runtime import herdr_call

    return herdr_call(args)


class RouterRuntime:
    """Address agents by name, route their operations through the runtime router and list herdr panes."""

    def __init__(
        self,
        router: RuntimeRouter,
        agents: Callable[[str], AgentRecord | None],
        herdr: Callable[[list[str]], dict[str, object]] = _herdr,
        homes: Callable[[AgentRecord], tuple[Path, ...]] = lambda agent: (),
    ) -> None:
        self.router, self.agents, self.herdr, self.homes = router, agents, herdr, homes

    def _missing(self, operation: str, handle: str) -> Outcome:
        return Outcome(operation, Status.UNAVAILABLE, "", detail=f"no agent named {handle}")

    def inspect(self, handle: str, *, deadline: Deadline) -> Outcome:
        _live(deadline)
        agent = self.agents(handle)
        return self._missing("inspect", handle) if agent is None else self.router.observe(agent)

    def launch(self, request: Mapping[str, object], *, deadline: Deadline) -> Outcome:
        _live(deadline)
        return self.router.spawn(SpawnRequest(**request))

    def stop(self, handle: str, *, deadline: Deadline) -> Outcome:
        _live(deadline)
        agent = self.agents(handle)
        return self._missing("stop", handle) if agent is None else self.router.terminate(agent, self.homes(agent))

    def panes(self, *, deadline: Deadline) -> list[dict[str, object]]:
        _live(deadline)
        listed = self.herdr(["pane", "list"]).get("panes", [])
        return list(listed) if isinstance(listed, list) else []
