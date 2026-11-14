from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from scripts.inbox.store import InboxError, close_reason
from scripts.swarm.store import AgentRecord
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Tools, Unsupported
from scripts.tick.ports.source_control import (
    Capabilities,
    ChangeRef,
    ChangeState,
    CheckState,
    Comment,
    Merged,
    OpenChange,
    QueueEntry,
    QueueHealth,
    RateLimit,
    RepositoryRun,
    Run,
    Tag,
    WorkflowRun,
)

if TYPE_CHECKING:
    from scripts.swarm.store import SwarmConfig


__all__ = [
    "Call",
    "FakeCoordination",
    "FakeHost",
    "FakeInbox",
    "FakeLedger",
    "FakeLlm",
    "FakeMetrics",
    "FakePort",
    "FakeProcesses",
    "FakeRuntime",
    "FakeSourceControl",
    "fake_tools",
]


@dataclass(frozen=True)
class Call:
    operation: str
    args: tuple[object, ...]
    deadline: Deadline


class FakePort(Protocol):
    answers: dict[str, object]
    calls: list[Call]


def _record(port: FakePort, operation: str, args: tuple[object, ...], deadline: Deadline) -> Any:
    if deadline.expired:
        raise TimeoutError("tool deadline expired")
    port.calls.append(Call(operation, args, deadline))
    return port.answers.get(operation)


@dataclass
class FakeLedger:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def export(self, *, deadline: Deadline) -> object:
        return _record(self, "export", (), deadline)

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object:
        return _record(self, "mutate", (operations,), deadline)

    def update_task(
        self,
        task_id: str,
        fields: Mapping[str, object],
        *,
        if_state: Sequence[str] = (),
        if_match: Mapping[str, object] | None = None,
        deadline: Deadline,
    ) -> dict[str, Any]:
        return _record(self, "update_task", (task_id, fields, if_state, if_match), deadline)

    def task(self, task_id: str, *, deadline: Deadline) -> dict[str, Any]:
        return _record(self, "task", (task_id,), deadline)

    def watchdog_copy(self, *, deadline: Deadline) -> dict[str, Any]:
        return _record(self, "watchdog_copy", (), deadline)

    def notify(self, text: str, *, deadline: Deadline) -> None:
        _record(self, "notify", (text,), deadline)

    def question(self, text: str, by: str, op_id: str, *, deadline: Deadline) -> None:
        _record(self, "question", (text, by, op_id), deadline)

    def comment(self, task_id: str, text: str, by: str, *, deadline: Deadline) -> None:
        _record(self, "comment", (task_id, text, by), deadline)


@dataclass
class FakeCoordination:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def read(self, *, deadline: Deadline) -> object:
        return _record(self, "read", (), deadline)

    def lease(self, *, deadline: Deadline) -> object:
        return _record(self, "lease", (), deadline)

    def lock(self, name: str, *, deadline: Deadline) -> object:
        return _record(self, "lock", (name,), deadline)

    def tick_lock(self, held: object, ttl_ms: int, caller: str, *, deadline: Deadline) -> str | None:
        return _record(self, "tick_lock", (held, ttl_ms, caller), deadline)

    def release_tick_lock(self, token: str, caller: str, *, deadline: Deadline) -> None:
        _record(self, "release_tick_lock", (token, caller), deadline)

    def configure(self, settings: "SwarmConfig", *, deadline: Deadline) -> object:
        return _record(self, "configure", (settings,), deadline)

    def capacity(self, *, deadline: Deadline) -> object:
        return _record(self, "capacity", (), deadline)

    def mark(self, name: str, value: object, *, deadline: Deadline) -> object:
        return _record(
            self,
            "mark",
            (
                name,
                value,
            ),
            deadline,
        )

    def marked(self, name: str, *, deadline: Deadline) -> str | None:
        return _record(self, "marked", (name,), deadline)

    def store_read(
        self, operation: str, args: Sequence[object], kwargs: Mapping[str, object], *, deadline: Deadline
    ) -> Any:
        """Record a store read and answer the ``store:<operation>`` answer."""
        _record(self, "store_read", (operation, tuple(args), dict(kwargs)), deadline)
        return self.answers.get(f"store:{operation}")

    def store_write(
        self, operation: str, args: Sequence[object], kwargs: Mapping[str, object], *, deadline: Deadline
    ) -> Any:
        """Record a store write and answer the ``store:<operation>`` answer."""
        _record(self, "store_write", (operation, tuple(args), dict(kwargs)), deadline)
        return self.answers.get(f"store:{operation}")


@dataclass
class FakeSourceControl:
    """Source control fake that records calls and answers Unsupported for queue operations without a queue."""

    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    merge_queue: bool = False

    def _queued(self, operation: str, answer: Any, default: Any) -> Any:
        """Return the supplied answer, else the queue default, or Unsupported when the fake has no queue."""
        if operation in self.answers:
            return answer
        return default if self.merge_queue else Unsupported(operation)

    def change(self, reference: ChangeRef, *, deadline: Deadline) -> ChangeState:
        return _record(self, "change", (reference,), deadline)

    def checks(self, reference: ChangeRef, *, deadline: Deadline) -> tuple[CheckState, ...]:
        return _record(self, "checks", (reference,), deadline) or ()

    def runs(self, reference: ChangeRef, *, deadline: Deadline) -> tuple[Run, ...]:
        return _record(self, "runs", (reference,), deadline) or ()

    def enqueue(self, reference: ChangeRef, *, deadline: Deadline) -> QueueEntry | Unsupported:
        answer = _record(self, "enqueue", (reference,), deadline)
        return self._queued("enqueue", answer, QueueEntry(reference, 1))

    def merge(self, reference: ChangeRef, *, deadline: Deadline) -> Merged:
        return _record(self, "merge", (reference,), deadline)

    def queue_health(self, *, deadline: Deadline) -> QueueHealth | Unsupported:
        return self._queued("queue_health", _record(self, "queue_health", (), deadline), QueueHealth(0, ()))

    def tags(self, *, deadline: Deadline) -> tuple[Tag, ...]:
        return _record(self, "tags", (), deadline) or ()

    def comment(self, reference: ChangeRef, body: str, *, deadline: Deadline) -> Comment:
        return _record(self, "comment", (reference, body), deadline)

    def capabilities(self, *, deadline: Deadline) -> Capabilities:
        answer = _record(self, "capabilities", (), deadline)
        return answer if answer is not None else Capabilities(merge_queue=self.merge_queue)

    def issue_state(self, reference: ChangeRef, *, deadline: Deadline) -> str:
        return _record(self, "issue_state", (reference,), deadline)

    def close_issue(self, reference: ChangeRef, *, deadline: Deadline) -> str:
        return _record(self, "close_issue", (reference,), deadline)

    def rate_limit(self, *, deadline: Deadline) -> RateLimit:
        return _record(self, "rate_limit", (), deadline)

    def issues_enabled(self, *, deadline: Deadline) -> bool:
        return _record(self, "issues_enabled", (), deadline)

    def latest_runs(self, *, deadline: Deadline) -> tuple[RepositoryRun, ...]:
        return _record(self, "latest_runs", (), deadline)

    def merged_at(self, commit: str, *, deadline: Deadline) -> str | None:
        return _record(self, "merged_at", (commit,), deadline)

    def open_changes(self, *, deadline: Deadline) -> tuple[OpenChange, ...]:
        return _record(self, "open_changes", (), deadline)

    def cancel_run(self, run_id: int, *, deadline: Deadline) -> None:
        return _record(self, "cancel_run", (run_id,), deadline)

    def workflow_runs(
        self, workflow: str, event: str, conclusion: str, since: str, *, deadline: Deadline
    ) -> tuple[WorkflowRun, ...]:
        return _record(self, "workflow_runs", (workflow, event, conclusion, since), deadline)

    def download(self, path: str, *, deadline: Deadline) -> bytes:
        return _record(self, "download", (path,), deadline)

    def graph(self, document: str, variables: Mapping[str, str], *, deadline: Deadline) -> Any:
        return _record(self, "graph", (document, variables), deadline)


@dataclass
class FakeRuntime:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def inspect(self, handle: str, *, deadline: Deadline) -> object:
        return _record(self, "inspect", (handle,), deadline)

    def launch(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return _record(self, "launch", (request,), deadline)

    def stop(self, handle: str, *, deadline: Deadline) -> object:
        return _record(self, "stop", (handle,), deadline)

    def panes(self, *, deadline: Deadline) -> object:
        return _record(self, "panes", (), deadline)

    def live_names(self, *, deadline: Deadline) -> set[str]:
        return _record(self, "live_names", (), deadline)

    def observe(self, agent: AgentRecord, *, deadline: Deadline) -> Any:
        return _record(self, "observe", (agent,), deadline)

    def retire(self, agent: AgentRecord, *, homes: Sequence[Path] = (), deadline: Deadline) -> bool:
        return _record(self, "retire", (agent, homes), deadline)

    def name_pane(self, agent: AgentRecord, *, deadline: Deadline) -> None:
        _record(self, "name_pane", (agent,), deadline)

    def refusal(self, agent: AgentRecord, *, deadline: Deadline) -> str:
        return _record(self, "refusal", (agent,), deadline)

    def resume(self, config: "SwarmConfig", agent: AgentRecord, text: str, *, deadline: Deadline) -> Any:
        return _record(self, "resume", (config, agent, text), deadline)

    def dismiss(self, agent: AgentRecord, *, deadline: Deadline) -> bool:
        return _record(self, "dismiss", (agent,), deadline)

    def fence(self, held: object, deployment: str, *, deadline: Deadline) -> Any:
        return _record(self, "fence", (held, deployment), deadline)


@dataclass
class FakeProcesses:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def scan(self, *, deadline: Deadline) -> object:
        return _record(self, "scan", (), deadline)

    def rescan(self, *, deadline: Deadline) -> object:
        return _record(self, "rescan", (), deadline)


@dataclass
class FakeInbox:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    information: set[str] = field(default_factory=set)

    def read(self, address: str, *, deadline: Deadline) -> object:
        return _record(self, "read", (address,), deadline)

    def send(self, address: str, body: str, *, deadline: Deadline) -> object:
        return _record(
            self,
            "send",
            (
                address,
                body,
            ),
            deadline,
        )

    def close(self, item_id: str, outcome: str, detail: str = "", *, deadline: Deadline) -> object:
        """Refuse an unknown outcome, a missing detail or a bare done on a work item, as the inbox store does."""
        state, reason = close_reason(outcome, detail)
        if state == "done" and reason == "done" and item_id not in self.information:
            raise InboxError("done needs an outcome naming where the work went")
        return _record(self, "close", (item_id, outcome, detail), deadline)

    def settle_exit(self, name: str, seat: str, exit_text: str, *, deadline: Deadline) -> object:
        return _record(self, "settle_exit", (name, seat, exit_text), deadline)


@dataclass
class FakeLlm:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def classify(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return _record(self, "classify", (request,), deadline)


@dataclass
class FakeMetrics:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def emit(self, measurements: Mapping[str, object], *, deadline: Deadline) -> object:
        return _record(self, "emit", (measurements,), deadline)

    def log(self, slug: str, rows: Sequence[object], *, deadline: Deadline) -> object:
        return _record(self, "log", (slug, tuple(rows)), deadline)


@dataclass
class FakeHost:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def read(self, name: str, *, deadline: Deadline) -> object:
        return _record(self, "read", (name,), deadline)

    def run(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return _record(self, "run", (request,), deadline)


def fake_tools(effects: frozenset[str] | None = None) -> Tools:
    tools = Tools(
        ledger=FakeLedger({"export": {}}),
        coordination=FakeCoordination(),
        source_control=FakeSourceControl(),
        runtime=FakeRuntime(),
        processes=FakeProcesses({"scan": ()}),
        inbox=FakeInbox(),
        llm=FakeLlm(),
        metrics=FakeMetrics(),
        host=FakeHost(),
    )
    return tools if effects is None else tools.for_effects(effects)
