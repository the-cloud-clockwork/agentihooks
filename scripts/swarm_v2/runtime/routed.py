"""The swarm tick's runtime: spawn, observe, command, retire, reattach and resume go through the runtime protocol;
inspection calls (live names, bindings, refusals, status, conversations) stay on the local herdr runtime."""

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from scripts.swarm import lease, ledger_client
from scripts.swarm.pane import PaneObservation
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER, AgentRecord, RedisStore
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2.runtime.base import (
    HIVE_LANES,
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
        if lane == MASTER:
            ledger_client.LedgerClient(service=True).join(config.slug, name, "orchestrator")
        request = SpawnRequest(config, lane, name, task)
        placed = self.launch is not None and self.router.spawn_backend(request) != LOCAL
        outcome = self.launch(request) if placed else self.router.spawn(request)
        if not outcome.ok:
            fields = outcome.value if isinstance(outcome.value, dict) else {}
            raise SpawnError(
                outcome.detail, outcome.status, fields.get("pane_id", ""), harness=fields.get("harness", "")
            )
        return outcome.value

    def observe(self, agent: AgentRecord) -> PaneObservation:
        observed = self.router.observe(agent).value
        return PaneObservation("unknown") if observed is None else observed

    def nudge(self, agent: AgentRecord, text: str) -> str | None:
        outcome = self.router.command(agent, text)
        return None if outcome.ok else outcome.detail or outcome.status.value

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
        if agent.lane == MASTER:
            ledger_client.LedgerClient(service=True).join(config.slug, agent.name, "orchestrator")
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
    runtimes = [LocalHerdrRuntime(herdr)] + ([kubernetes] if kubernetes is not None else [])
    placement = Placement(kubernetes.backend) if kubernetes is not None else None
    router = RuntimeRouter.from_environ(runtimes, os.environ if environ is None else environ, placement)
    return RoutedRuntime(herdr, router, launch)


def hive_controller(runtime: RoutedRuntime, store: RedisStore, slug: str, held: lease.Lease) -> RoutedRuntime:
    from scripts.hive.launch import HIVE, HiveLaunch
    from scripts.hive.reports import Reports

    runtime.herdr_runtime.hive = Reports(store.redis)
    hive = HiveLaunch(store, slug, held, runtime.herdr_runtime)
    router = RuntimeRouter(
        [*runtime.router.runtimes.values(), hive],
        default=HIVE,
        disabled=runtime.router.disabled,
        placement=Placement(HIVE, lanes=HIVE_LANES, local_profiles=frozenset()),
    )
    placed = RoutedRuntime(runtime.herdr_runtime, router)
    placed.hive_launch = hive
    placed.original_runtime = runtime
    return placed
