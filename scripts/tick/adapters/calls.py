"""Bind tick tool ports to a step deadline for legacy reap helpers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm.store import AgentRecord, SwarmConfig
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Ledger, Runtime


@dataclass(frozen=True)
class RuntimeCalls:
    port: Runtime
    deadline: Deadline

    def live_names(self) -> set[str]:
        return self.port.live_names(deadline=self.deadline)

    def observe(self, agent: AgentRecord) -> Any:
        return self.port.observe(agent, deadline=self.deadline)

    def retire(self, agent: AgentRecord, homes: Sequence[Path] = ()) -> bool:
        return self.port.retire(agent, homes=homes, deadline=self.deadline)

    def name_pane(self, agent: AgentRecord) -> None:
        self.port.name_pane(agent, deadline=self.deadline)

    def refusal(self, agent: AgentRecord) -> str:
        return self.port.refusal(agent, deadline=self.deadline)

    def resume(self, config: SwarmConfig, agent: AgentRecord, text: str) -> Any:
        return self.port.resume(config, agent, text, deadline=self.deadline)

    def dismiss(self, agent: AgentRecord) -> bool:
        return self.port.dismiss(agent, deadline=self.deadline)


@dataclass(frozen=True)
class AdmissionCalls:
    """The pending admission gate of the spawn helpers, over the runtime port."""

    port: Runtime
    deadline: Deadline

    def admit(self, slug: str, tasks: Sequence[Mapping[str, Any]], now_ms: int) -> Any:
        return self.port.admit(slug, tasks, now_ms, deadline=self.deadline)

    def release(self, slug: str, task_id: str, reservation: str) -> None:
        self.port.release_admission(slug, task_id, reservation, deadline=self.deadline)


@dataclass(frozen=True)
class _Router:
    placement: object


@dataclass(frozen=True)
class SpawnRuntimeCalls(RuntimeCalls):
    """The runtime the spawn helpers call, bound to the spawn recording deadline and reached only through the port."""

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: Mapping[str, Any]) -> Any:
        return self.port.place(config, lane, name, task, deadline=self.deadline)

    def has_capacity(self, config: SwarmConfig) -> bool:
        return self.port.has_capacity(config, deadline=self.deadline)

    def launch_room(self, config: SwarmConfig) -> int | None:
        return self.port.launch_room(config, deadline=self.deadline)

    def reported(self, agent: AgentRecord) -> bool:
        return self.port.reported(agent, deadline=self.deadline)

    def placement_refusal(self, lane: str, task: Mapping[str, Any]) -> str:
        return self.port.placement_refusal(lane, task, deadline=self.deadline)

    def begin_placement_pass(self) -> None:
        self.port.begin_placement(deadline=self.deadline)

    def end_placement_pass(self) -> None:
        self.port.end_placement(deadline=self.deadline)

    def reap_name(self, name: str) -> bool:
        return self.port.reap_name(name, deadline=self.deadline)

    @property
    def router(self) -> _Router:
        return _Router(self.port.placement(deadline=self.deadline))

    @property
    def admission(self) -> AdmissionCalls | None:
        return AdmissionCalls(self.port, self.deadline) if self.port.admits(deadline=self.deadline) else None


@dataclass(frozen=True)
class LedgerCalls:
    port: Ledger
    deadline: Deadline

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: Mapping[str, object],
        if_state: Sequence[str] = (),
        if_match: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        return self.port.update_task(task_id, fields, if_state=if_state, if_match=if_match, deadline=self.deadline)

    def task(self, slug: str, task_id: str) -> dict[str, Any]:
        return self.port.task(task_id, deadline=self.deadline)

    def notify(self, slug: str, text: str) -> None:
        self.port.notify(text, deadline=self.deadline)

    def question(self, slug: str, text: str, by: str, op_id: str) -> None:
        self.port.question(text, by, op_id, deadline=self.deadline)

    def comment(self, slug: str, task_id: str, text: str, by: str) -> None:
        self.port.comment(task_id, text, by, deadline=self.deadline)
