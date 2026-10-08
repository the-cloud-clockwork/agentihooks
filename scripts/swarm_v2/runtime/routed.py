"""The swarm tick's runtime: spawn, observe, command, retire, reattach and resume go through the runtime protocol;
inspection calls (live names, bindings, refusals, status, conversations) stay on the local herdr runtime."""

import os
from collections.abc import Mapping, Sequence
from typing import Any

from scripts.swarm.pane import PaneObservation
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2.runtime.base import Recovery, RuntimeRouter, SpawnRequest
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime


class RoutedRuntime:
    def __init__(self, herdr: HerdrRuntime, router: RuntimeRouter):
        self.herdr_runtime, self.router = herdr, router

    def __getattr__(self, name: str) -> Any:
        return getattr(self.herdr_runtime, name)

    def spawn(self, config: Any, lane: str, name: str, task: dict) -> Placed:
        outcome = self.router.spawn(SpawnRequest(config, lane, name, task))
        if not outcome.ok:
            raise SpawnError(outcome.detail, outcome.status)
        return outcome.value

    def observe(self, agent: AgentRecord) -> PaneObservation:
        observed = self.router.observe(agent).value
        return PaneObservation("unknown") if observed is None else observed

    def nudge(self, agent: AgentRecord, text: str) -> None:
        self.router.command(agent, text)

    def retire(self, agent: AgentRecord, homes: Sequence[str] = ()) -> bool:
        return self.router.terminate(agent, tuple(homes)).ok

    def recover(self, name: str) -> Placed:
        outcome = self.router.recover(AgentRecord(name, MASTER, MASTER), Recovery.REATTACH)
        return outcome.value if outcome.ok else Placed("", "")

    def resume(self, config: Any, agent: AgentRecord, text: str) -> Placed:
        outcome = self.router.recover(agent, Recovery.RESUME, config, text)
        if not outcome.ok:
            raise SpawnError(outcome.detail, outcome.status)
        return outcome.value


def routed(environ: Mapping[str, str] | None = None, herdr: HerdrRuntime | None = None) -> RoutedRuntime:
    herdr = HerdrRuntime() if herdr is None else herdr
    router = RuntimeRouter.from_environ([LocalHerdrRuntime(herdr)], os.environ if environ is None else environ)
    return RoutedRuntime(herdr, router)
