from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from scripts.tick.deadline import Deadline
from scripts.tick.ports import Tools, Unsupported


@dataclass(frozen=True)
class Call:
    operation: str
    args: tuple[object, ...]
    deadline: Deadline


@dataclass
class FakePort:
    answers: dict[str, object] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def _record(self, operation: str, args: tuple[object, ...], deadline: Deadline) -> object:
        if deadline.expired:
            raise TimeoutError("tool deadline expired")
        self.calls.append(Call(operation, args, deadline))
        return self.answers.get(operation)


class FakeLedger(FakePort):
    def export(self, *, deadline: Deadline) -> object:
        return self._record("export", (), deadline)

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object:
        return self._record("mutate", (operations,), deadline)


class FakeCoordination(FakePort):
    def read(self, *, deadline: Deadline) -> object:
        return self._record("read", (), deadline)

    def lease(self, *, deadline: Deadline) -> object:
        return self._record("lease", (), deadline)

    def lock(self, name: str, *, deadline: Deadline) -> object:
        return self._record("lock", (name,), deadline)

    def configure(self, settings: Mapping[str, object], *, deadline: Deadline) -> object:
        return self._record("configure", (settings,), deadline)

    def capacity(self, *, deadline: Deadline) -> object:
        return self._record("capacity", (), deadline)

    def mark(self, name: str, value: object, *, deadline: Deadline) -> object:
        return self._record(
            "mark",
            (
                name,
                value,
            ),
            deadline,
        )


class FakeSourceControl(FakePort):
    def change(self, reference: str, *, deadline: Deadline) -> object:
        return self._record("change", (reference,), deadline)

    def checks(self, reference: str, *, deadline: Deadline) -> object:
        return self._record("checks", (reference,), deadline)

    def runs(self, reference: str, *, deadline: Deadline) -> object:
        return self._record("runs", (reference,), deadline)

    def enqueue(self, reference: str, *, deadline: Deadline) -> object:
        return self._record("enqueue", (reference,), deadline)

    def merge(self, reference: str, *, deadline: Deadline) -> object:
        return self._record("merge", (reference,), deadline)

    def queue_health(self, *, deadline: Deadline) -> object:
        return self._record("queue_health", (), deadline)

    def tags(self, *, deadline: Deadline) -> object:
        return self._record("tags", (), deadline)

    def comment(self, reference: str, body: str, *, deadline: Deadline) -> object:
        return self._record(
            "comment",
            (
                reference,
                body,
            ),
            deadline,
        )

    def capabilities(self, *, deadline: Deadline) -> object:
        return self._record("capabilities", (), deadline)


class FakeRuntime(FakePort):
    def inspect(self, handle: str, *, deadline: Deadline) -> object:
        return self._record("inspect", (handle,), deadline)

    def launch(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return self._record("launch", (request,), deadline)

    def stop(self, handle: str, *, deadline: Deadline) -> object:
        return self._record("stop", (handle,), deadline)

    def panes(self, *, deadline: Deadline) -> object:
        return self._record("panes", (), deadline)


class FakeProcesses(FakePort):
    def scan(self, *, deadline: Deadline) -> object:
        return self._record("scan", (), deadline)


class FakeInbox(FakePort):
    def read(self, address: str, *, deadline: Deadline) -> object:
        return self._record("read", (address,), deadline)

    def send(self, address: str, body: str, *, deadline: Deadline) -> object:
        return self._record(
            "send",
            (
                address,
                body,
            ),
            deadline,
        )

    def close(self, item_id: str, *, deadline: Deadline) -> object:
        return self._record("close", (item_id,), deadline)


class FakeLlm(FakePort):
    def classify(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return self._record("classify", (request,), deadline)


class FakeMetrics(FakePort):
    def emit(self, measurements: Mapping[str, object], *, deadline: Deadline) -> object:
        return self._record("emit", (measurements,), deadline)


class FakeHost(FakePort):
    def read(self, name: str, *, deadline: Deadline) -> object:
        return self._record("read", (name,), deadline)

    def run(self, request: Mapping[str, object], *, deadline: Deadline) -> object:
        return self._record("run", (request,), deadline)


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
