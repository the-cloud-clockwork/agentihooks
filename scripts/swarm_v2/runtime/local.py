"""The local herdr runtime behind the runtime protocol: each operation runs the existing HerdrRuntime call."""

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from hooks.proc import Process, processes
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import SpawnError
from scripts.swarm_v2.runtime import process
from scripts.swarm_v2.runtime.base import (
    LOCAL,
    Capability,
    Outcome,
    Recovery,
    SpawnRequest,
    Status,
    Unqualified,
    foreign,
    legacy,
)


def _failed(operation: str, exc: SpawnError) -> Outcome:
    status = Status.AMBIGUOUS if isinstance(exc.__cause__, subprocess.TimeoutExpired) else Status(exc.status)
    return Outcome(operation, status, LOCAL, detail=str(exc))


class LocalHerdrRuntime:
    backend = LOCAL
    capabilities = frozenset(
        {
            Capability.SPAWN,
            Capability.OBSERVE,
            Capability.COMMAND,
            Capability.TERMINATE,
            Capability.RECOVER,
            Capability.NATIVE_RESUME,
        }
    )

    def __init__(
        self,
        herdr: HerdrRuntime,
        namespace: Callable[[], str] = process.local_namespace,
        table: Callable[[], Mapping[int, Process]] = processes,
    ):
        self.herdr, self.namespace, self.table = herdr, namespace, table

    def spawn(self, request: SpawnRequest) -> Outcome:
        try:
            placed = self.herdr.spawn(request.config, request.lane, request.name, request.task)
        except SpawnError as exc:
            return _failed("spawn", exc)
        return Outcome("spawn", Status.OK, LOCAL, placed)

    def admit(self, store: RedisStore, slug: str, agent: AgentRecord, previous_execution_id: str = "") -> AgentRecord:
        pid = agent.profile_decision.get("validation", {}).get("pid")
        target = process.launch_target(pid, self.namespace(), self.table())
        return store.start_execution(
            slug, replace(agent, runtime_backend=LOCAL, runtime_target=target), previous_execution_id
        )

    def observe(self, agent: AgentRecord) -> Outcome:
        refused = foreign(self, "observe", agent)
        if refused:
            return refused
        observed = self.herdr.observe(agent)
        status = Status.AMBIGUOUS if observed.state == "unknown" else Status.OK
        return Outcome("observe", status, LOCAL, observed)

    def command(self, agent: AgentRecord, text: str) -> Outcome:
        refused = foreign(self, "command", agent)
        if refused:
            return refused
        self.herdr.nudge(agent, text)
        return Outcome("command", Status.OK, LOCAL, "accepted")

    def drain(self, agent: AgentRecord) -> Outcome:
        return Outcome("drain", Status.UNSUPPORTED, LOCAL, detail="local herdr has no drain")

    def terminate(self, agent: AgentRecord, homes: tuple = ()) -> Outcome:
        refused = foreign(self, "terminate", agent)
        if refused:
            return refused
        if legacy(agent):
            retired = self.herdr.retire(agent, homes=homes)
        else:
            pid = process.resolve(agent, self.namespace(), self.table())
            if isinstance(pid, Unqualified):
                return Outcome("terminate", Status.REFUSED, LOCAL, pid, pid.value)
            retired = self.herdr.retire_process(agent, pid, homes, agent.runtime_target["pid_start"])
        if retired:
            return Outcome("terminate", Status.OK, LOCAL)
        refusal = self.herdr.refusal(agent)
        return Outcome("terminate", Status.REFUSED, LOCAL, refusal, refusal["refusal"])

    def recover(self, agent: AgentRecord, mode: Recovery, config: Any, text: str) -> Outcome:
        refused = foreign(self, "recover", agent)
        if refused:
            return refused
        if mode is Recovery.REATTACH:
            placed = self.herdr.recover(agent.name)
            status = Status.OK if placed.pane_id else Status.UNAVAILABLE
            return Outcome("recover", status, LOCAL, placed)
        try:
            placed = self.herdr.resume(config, agent, text)
        except SpawnError as exc:
            return _failed("recover", exc)
        return Outcome("recover", Status.OK, LOCAL, placed)
