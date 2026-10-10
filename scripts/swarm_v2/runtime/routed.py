"""The swarm tick's runtime: spawn, observe, command, retire, reattach and resume go through the runtime protocol;
inspection calls (live names, bindings, refusals, status, conversations) stay on the local herdr runtime."""

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from scripts.swarm.pane import PaneObservation
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2.runtime.base import (
    LOCAL,
    Outcome,
    Placement,
    Recovery,
    Runtime,
    RuntimeRouter,
    SpawnRequest,
    Unqualified,
)
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

Launcher = Callable[[SpawnRequest], Outcome]
NO_LAUNCH = "a Kubernetes runtime needs a distributed launch"


class RoutedRuntime:
    def __init__(self, herdr: HerdrRuntime, router: RuntimeRouter, launch: Launcher | None = None):
        self.herdr_runtime, self.router, self.launch, self.refused = herdr, router, launch, {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.herdr_runtime, name)

    def spawn(self, config: Any, lane: str, name: str, task: dict) -> Placed:
        request = SpawnRequest(config, lane, name, task)
        placed = self.launch is not None and self.router.spawn_backend(request) != LOCAL
        outcome = self.launch(request) if placed else self.router.spawn(request)
        if not outcome.ok:
            raise SpawnError(outcome.detail, outcome.status)
        return outcome.value

    def observe(self, agent: AgentRecord) -> PaneObservation:
        observed = self.router.observe(agent).value
        return PaneObservation("unknown") if observed is None else observed

    def nudge(self, agent: AgentRecord, text: str) -> None:
        self.router.command(agent, text)

    def retire(self, agent: AgentRecord, homes: Sequence[str] = ()) -> bool:
        outcome = self.router.terminate(agent, tuple(homes))
        if isinstance(outcome.value, Unqualified):
            self.refused[agent.name] = {"process": 0, "refusal": outcome.detail}
        else:
            self.refused.pop(agent.name, None)
        return outcome.ok

    def refusal(self, agent: AgentRecord) -> dict:
        return self.refused.get(agent.name) or self.herdr_runtime.refusal(agent)

    def recover(self, name: str) -> Placed:
        outcome = self.router.recover(AgentRecord(name, MASTER, MASTER), Recovery.REATTACH)
        return outcome.value if outcome.ok else Placed("", "")

    def resume(self, config: Any, agent: AgentRecord, text: str) -> Placed:
        outcome = self.router.recover(agent, Recovery.RESUME, config, text)
        if not outcome.ok:
            raise SpawnError(outcome.detail, outcome.status)
        return outcome.value


def routed(
    environ: Mapping[str, str] | None = None,
    herdr: HerdrRuntime | None = None,
    kubernetes: Runtime | None = None,
    launch: Launcher | None = None,
) -> RoutedRuntime:
    if kubernetes is not None and launch is None:
        raise ValueError(NO_LAUNCH)
    herdr = HerdrRuntime() if herdr is None else herdr
    runtimes = [LocalHerdrRuntime(herdr), *([kubernetes] if kubernetes else [])]
    placement = Placement(kubernetes.backend) if kubernetes else None
    router = RuntimeRouter.from_environ(runtimes, os.environ if environ is None else environ, placement)
    return RoutedRuntime(herdr, router, launch)
