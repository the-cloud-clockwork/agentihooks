from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from scripts.tick.deadline import Deadline
from scripts.tick.ports import Tools, Unsupported


@dataclass(frozen=True)
class Call:
    operation: str
    args: tuple[object, ...]
    deadline: Deadline


class FakePort(Protocol):
    answers: dict[str, object]
    calls: list[Call]


def _record(port: FakePort, operation: str, args: tuple[object, ...], deadline: Deadline) -> object:
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

    def configure(self, settings: Mapping[str, object], *, deadline: Deadline) -> object:
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


@dataclass
class FakeSourceControl:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def change(self, reference: str, *, deadline: Deadline) -> object:
        return _record(self, "change", (reference,), deadline)

    def checks(self, reference: str, *, deadline: Deadline) -> object:
        return _record(self, "checks", (reference,), deadline)

    def runs(self, reference: str, *, deadline: Deadline) -> object:
        return _record(self, "runs", (reference,), deadline)

    def enqueue(self, reference: str, *, deadline: Deadline) -> object:
        return _record(self, "enqueue", (reference,), deadline)

    def merge(self, reference: str, *, deadline: Deadline) -> object:
        return _record(self, "merge", (reference,), deadline)

    def queue_health(self, *, deadline: Deadline) -> object:
        return _record(self, "queue_health", (), deadline)

    def tags(self, *, deadline: Deadline) -> object:
        return _record(self, "tags", (), deadline)

    def comment(self, reference: str, body: str, *, deadline: Deadline) -> object:
        return _record(
            self,
            "comment",
            (
                reference,
                body,
            ),
            deadline,
        )

    def capabilities(self, *, deadline: Deadline) -> object:
        return _record(self, "capabilities", (), deadline)


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


@dataclass
class FakeProcesses:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def scan(self, *, deadline: Deadline) -> object:
        return _record(self, "scan", (), deadline)


@dataclass
class FakeInbox:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

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

    def close(self, item_id: str, *, deadline: Deadline) -> object:
        return _record(self, "close", (item_id,), deadline)


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


@dataclass
class FakeHost:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def read(self, name: str, *, deadline: Deadline) -> object:
        return _record(self, "read", (name,), deadline)

    def run(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return _record(self, "run", (request,), deadline)


def fake_tools() -> Tools:
    return Tools(
        ledger=FakeLedger({"export": {}}),
        coordination=FakeCoordination(),
        source_control=FakeSourceControl({"queue_health": Unsupported("queue_health")}),
        runtime=FakeRuntime(),
        processes=FakeProcesses({"scan": ()}),
        inbox=FakeInbox(),
        llm=FakeLlm(),
        metrics=FakeMetrics(),
        host=FakeHost(),
    )
