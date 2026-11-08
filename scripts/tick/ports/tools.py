from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace

from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import (
    Coordination,
    Host,
    Inbox,
    Ledger,
    Llm,
    Metrics,
    Processes,
    Runtime,
    SourceControl,
    UndeclaredEffect,
)

_OPERATIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "ledger": (
        frozenset([Ledger.export.__name__, Ledger.task.__name__]),
        frozenset(
            [
                Ledger.mutate.__name__,
                Ledger.update_task.__name__,
                Ledger.notify.__name__,
                Ledger.question.__name__,
                Ledger.comment.__name__,
            ]
        ),
    ),
    "coordination": (
        frozenset([Coordination.read.__name__, Coordination.capacity.__name__]),
        frozenset(
            [
                Coordination.lease.__name__,
                Coordination.lock.__name__,
                Coordination.configure.__name__,
                Coordination.mark.__name__,
            ]
        ),
    ),
    "source_control": (
        frozenset(
            [
                SourceControl.change.__name__,
                SourceControl.checks.__name__,
                SourceControl.runs.__name__,
                SourceControl.queue_health.__name__,
                SourceControl.tags.__name__,
                SourceControl.capabilities.__name__,
                SourceControl.issue_state.__name__,
                SourceControl.rate_limit.__name__,
                SourceControl.issues_enabled.__name__,
                SourceControl.latest_runs.__name__,
                SourceControl.merged_at.__name__,
                SourceControl.open_changes.__name__,
                SourceControl.workflow_runs.__name__,
                SourceControl.download.__name__,
                SourceControl.graph.__name__,
            ]
        ),
        frozenset(
            [
                SourceControl.enqueue.__name__,
                SourceControl.merge.__name__,
                SourceControl.comment.__name__,
                SourceControl.close_issue.__name__,
                SourceControl.cancel_run.__name__,
            ]
        ),
    ),
    "runtime": (
        frozenset(
            [
                Runtime.inspect.__name__,
                Runtime.panes.__name__,
                Runtime.live_names.__name__,
                Runtime.observe.__name__,
                Runtime.refusal.__name__,
                Runtime.has_capacity.__name__,
                Runtime.launch_room.__name__,
                Runtime.reported.__name__,
                Runtime.placement_refusal.__name__,
                Runtime.placement.__name__,
                Runtime.admits.__name__,
            ]
        ),
        frozenset(
            [
                Runtime.launch.__name__,
                Runtime.stop.__name__,
                Runtime.retire.__name__,
                Runtime.name_pane.__name__,
                Runtime.resume.__name__,
                Runtime.dismiss.__name__,
                Runtime.place.__name__,
                Runtime.begin_placement.__name__,
                Runtime.end_placement.__name__,
                Runtime.reap_name.__name__,
                Runtime.admit.__name__,
                Runtime.release_admission.__name__,
            ]
        ),
    ),
    "processes": (frozenset([Processes.scan.__name__]), frozenset()),
    "inbox": (frozenset([Inbox.read.__name__]), frozenset([Inbox.send.__name__, Inbox.close.__name__])),
    "llm": (frozenset(), frozenset([Llm.classify.__name__])),
    "metrics": (frozenset(), frozenset([Metrics.emit.__name__])),
    "host": (frozenset([Host.read.__name__]), frozenset([Host.run.__name__])),
}


@dataclass(frozen=True)
class _RestrictedPort:
    adapter: object
    name: str
    allowed: bool

    def __getattr__(self, operation: str) -> Callable[..., object]:
        reads, writes = _OPERATIONS[self.name]
        if operation not in reads | writes:
            raise AttributeError(operation)

        def invoke(*args: object, **kwargs: object) -> object:
            if operation in writes and not self.allowed:
                raise UndeclaredEffect(f"{self.name} is not a declared effect")
            deadline = kwargs.get("deadline")
            if not isinstance(deadline, Deadline):
                raise TypeError("tools require a Deadline keyword argument")
            if deadline.expired:
                raise TimeoutError("tool deadline expired")
            method: Callable[..., object] = getattr(self.adapter, operation)
            return method(*args, **kwargs)

        return invoke


@dataclass(frozen=True)
class Tools:
    ledger: Ledger
    coordination: Coordination
    source_control: SourceControl
    runtime: Runtime
    processes: Processes
    inbox: Inbox
    llm: Llm
    metrics: Metrics
    host: Host

    def for_effects(self, effects: frozenset[str]) -> "Tools":
        adapters = {
            "ledger": self.ledger,
            "coordination": self.coordination,
            "source_control": self.source_control,
            "runtime": self.runtime,
            "processes": self.processes,
            "inbox": self.inbox,
            "llm": self.llm,
            "metrics": self.metrics,
            "host": self.host,
        }
        ports = {name: _RestrictedPort(adapter, name, name in effects) for name, adapter in adapters.items()}
        return replace(self, **ports)


PASS_TOOLS: ContextVar[Tools | None] = ContextVar("tick_pass_tools", default=None)
