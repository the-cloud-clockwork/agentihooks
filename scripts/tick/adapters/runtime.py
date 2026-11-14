"""Runtime port over the runtime router for agent operations and herdr for the pane list."""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.swarm.store import AgentRecord, SwarmConfig
from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, SpawnRequest, Status
from scripts.tick.deadline import Deadline

__all__ = ["RouterRuntime"]


def _missing(operation: str, handle: str) -> Outcome:
    return Outcome(operation, Status.UNAVAILABLE, "", detail=f"no agent named {handle}")


def _herdr(args: list[str]) -> dict[str, object]:
    from scripts.swarm.runtime import herdr_call  # pyright: ignore[reportUnknownVariableType]

    answer: dict[str, object] = herdr_call(args)  # pyright: ignore[reportUnknownVariableType]
    return answer


class RouterRuntime:
    """Address agents by name, route their operations through the runtime router and list herdr panes."""

    def __init__(
        self,
        router: RuntimeRouter,
        agents: Callable[[str], AgentRecord | None],
        herdr: Callable[[list[str]], dict[str, object]] = _herdr,
        homes: Callable[[AgentRecord], tuple[Path, ...]] = lambda agent: (),
        legacy: Any = None,
        fencing: Callable[[object, str, Any], Any] | None = None,
    ) -> None:
        self.router, self.agents, self.herdr, self.homes = router, agents, herdr, homes
        self.legacy, self.fencing = legacy, fencing

    def inspect(self, handle: str, *, deadline: Deadline) -> Outcome:
        deadline.live()
        agent = self.agents(handle)
        return _missing("inspect", handle) if agent is None else self.router.observe(agent)

    def launch(self, request: Mapping[str, object], *, deadline: Deadline) -> Outcome:
        deadline.live()
        return self.router.spawn(SpawnRequest(**request))  # pyright: ignore[reportArgumentType]

    def stop(self, handle: str, *, deadline: Deadline) -> Outcome:
        deadline.live()
        agent = self.agents(handle)
        if agent is None:
            return _missing("stop", handle)
        return self.router.terminate(agent, self.homes(agent))  # pyright: ignore[reportUnknownMemberType]

    def panes(self, *, deadline: Deadline) -> list[dict[str, object]]:
        deadline.live()
        listed = self.herdr(["pane", "list"]).get("panes")
        panes: list[dict[str, object]] = listed if isinstance(listed, list) else []  # pyright: ignore[reportUnknownVariableType]
        return panes

    def live_names(self, *, deadline: Deadline) -> set[str]:
        deadline.live()
        names: set[str] = self.legacy.live_names()
        return names

    def observe(self, agent: AgentRecord, *, deadline: Deadline) -> Any:
        deadline.live()
        return self.legacy.observe(agent)

    def retire(self, agent: AgentRecord, *, homes: Sequence[Path] = (), deadline: Deadline) -> bool:
        deadline.live()
        retired: bool = self.legacy.retire(agent, homes=homes)
        return retired

    def name_pane(self, agent: AgentRecord, *, deadline: Deadline) -> None:
        deadline.live()
        self.legacy.name_pane(agent)

    def refusal(self, agent: AgentRecord, *, deadline: Deadline) -> str:
        deadline.live()
        refused: str = self.legacy.refusal(agent)
        return refused

    def resume(self, config: SwarmConfig, agent: AgentRecord, text: str, *, deadline: Deadline) -> Any:
        deadline.live()
        return self.legacy.resume(config, agent, text)

    def dismiss(self, agent: AgentRecord, *, deadline: Deadline) -> bool:
        deadline.live()
        dismissed: bool = self.legacy.dismiss(agent)
        return dismissed

    def fence(self, held: object, deployment: str, *, deadline: Deadline) -> Any:
        """Fence the pass runtime to the lease ``held`` in ``deployment``, keep it for every later operation and
        return it."""
        deadline.live()
        if self.fencing is None:
            raise RuntimeError("this runtime port has no fence")
        self.legacy = self.fencing(held, deployment, self.legacy)
        return self.legacy
