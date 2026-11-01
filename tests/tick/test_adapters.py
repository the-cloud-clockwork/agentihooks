"""Inbox, llm, metrics and host adapters delegate with deadlines and registered steps never import their modules."""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from hooks.classifier.result import DecisionRequest
from scripts.tick import ports
from scripts.tick.adapters import fakes, host, inbox, llm, metrics
from scripts.tick.deadline import Deadline

ROOT = Path(__file__).resolve().parents[2]
TICK = ROOT / "scripts" / "tick"
BANNED = (
    "scripts.inbox",
    "hooks.classifier",
    "scripts.swarm.metrics_outbox",
    "scripts.swarm.host_budget",
    "scripts.swarm.refresh_job",
    "scripts.swarm.snapshot_job",
)


def _open() -> Deadline:
    return Deadline(10.0, lambda: 4.0)


def _expired() -> Deadline:
    return Deadline(10.0, lambda: 10.0)


@dataclass
class _Store:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def inbox(self, address: str) -> list[str]:
        self.calls.append(("inbox", address))
        return [f"item for {address}"]

    def send(self, sender: str, address: str, text: str) -> str:
        self.calls.append(("send", sender, address, text))
        return "sent"

    def close(self, item_id: str, closer: str, kind: str, detail: str) -> str:
        self.calls.append(("close", item_id, closer, kind, detail))
        return "closed"


def test_inbox_delegates_each_operation_as_the_tick() -> None:
    store = _Store()
    port = inbox.StoreInbox(cast(Any, store), "tick@swarm")
    assert isinstance(port, ports.Inbox)
    assert port.read("eng-1@swarm", deadline=_open()) == ["item for eng-1@swarm"]
    assert port.send("eng-1@swarm", "wake", deadline=_open()) == "sent"
    assert port.close("abc", "handoff", "eng-2@swarm", deadline=_open()) == "closed"
    assert port.close("def", "cancel", deadline=_open()) == "closed"
    assert store.calls == [
        ("inbox", "eng-1@swarm"),
        ("send", "tick@swarm", "eng-1@swarm", "wake"),
        ("close", "abc", "tick@swarm", "handoff", "eng-2@swarm"),
        ("close", "def", "tick@swarm", "cancel", ""),
    ]


def _strict_store() -> Any:
    import fakeredis

    from scripts.inbox.store import InboxStore

    return InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


def test_inbox_closes_a_work_item_on_a_strict_store_only_with_an_outcome() -> None:
    from scripts.inbox.store import InboxError

    store = _strict_store()
    port = inbox.StoreInbox(store, "bob")
    bare = store.send("alice", "bob", "review my branch")
    with pytest.raises(InboxError, match="^done needs an outcome naming where the work went$"):
        port.close(bare.id, "done", deadline=_open())
    assert store.get(bare.id).state == "pending"
    work = store.send("alice", "bob", "fix the gate")
    closed = port.close(work.id, "done", "merged the fix", deadline=_open())
    assert (closed.state, closed.reason) == ("done", "done: merged the fix")
    note = store.send("alice", "bob", "status", fyi=True)
    assert port.close(note.id, "done", deadline=_open()).state == "done"


@pytest.mark.parametrize(
    "outcome,detail,message",
    [
        ("done", "", "^done needs an outcome naming where the work went$"),
        ("done", "  ", "^done needs an outcome naming where the work went$"),
        ("handoff", "", "^handoff needs the address the work went to$"),
        ("blocked", "", "^blocked needs what it is blocked on$"),
        ("finished", "x", "^closing needs a reason: one of done, handoff, blocked, cancel$"),
    ],
)
def test_fake_inbox_refuses_a_close_the_store_refuses(outcome: str, detail: str, message: str) -> None:
    from scripts.inbox.store import InboxError

    fake = fakes.FakeInbox()
    with pytest.raises(InboxError, match=message):
        fake.close("abc", outcome, detail, deadline=_open())
    assert fake.calls == []


def test_fake_inbox_records_a_close_with_an_outcome_and_an_information_item_bare() -> None:
    fake = fakes.FakeInbox(information={"note"})
    deadline = _open()
    assert fake.close("abc", "done", "merged", deadline=deadline) is None
    assert fake.close("def", "cancel", deadline=deadline) is None
    assert fake.close("note", "done", deadline=deadline) is None
    assert fake.calls == [
        fakes.Call("close", ("abc", "done", "merged"), deadline),
        fakes.Call("close", ("def", "cancel", ""), deadline),
        fakes.Call("close", ("note", "done", ""), deadline),
    ]


@pytest.mark.parametrize("operation,args", [("read", ("a",)), ("send", ("a", "b")), ("close", ("a", "cancel"))])
def test_inbox_refuses_an_expired_deadline_before_the_store(operation: str, args: tuple[str, ...]) -> None:
    store = _Store()
    port = inbox.StoreInbox(cast(Any, store), "tick@swarm")
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        getattr(port, operation)(*args, deadline=_expired())
    assert store.calls == []


@dataclass
class _Backend:
    model: str
    url: str
    timeout_s: float
    seen: list[object] = field(default_factory=list)

    def decide(self, request: object) -> str:
        self.seen.append(request)
        return "verdict"


def test_llm_classifies_through_a_backend_bounded_by_the_deadline() -> None:
    built: list[_Backend] = []

    def backend(model: str, url: str, timeout_s: float) -> _Backend:
        built.append(_Backend(model, url, timeout_s))
        return built[-1]

    port = llm.ApiLlm("liquid", "https://decide", backend)
    assert isinstance(port, ports.Llm)
    answer = port.classify({"state": {"task": "t"}, "questions": {"q": "?"}}, deadline=_open())
    assert answer == "verdict"
    assert [(item.model, item.url, item.timeout_s) for item in built] == [("liquid", "https://decide", 6.0)]
    request = built[0].seen[0]
    assert isinstance(request, DecisionRequest)
    assert (request.state, request.questions) == ({"task": "t"}, {"q": "?"})


def test_llm_refuses_an_expired_deadline_before_building_a_backend() -> None:
    built: list[object] = []
    port = llm.ApiLlm("liquid", "https://decide", cast(Any, lambda *args: built.append(args)))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        port.classify({"state": {}, "questions": {}}, deadline=_expired())
    assert built == []


def test_llm_defaults_to_the_decisions_api_backend() -> None:
    from hooks.classifier.api import DecisionsApiBackend

    assert llm.ApiLlm("liquid", "https://decide").backend is DecisionsApiBackend


@dataclass
class _Box:
    batches: list[list[tuple[object, list[dict]]]] = field(default_factory=list)
    checkpoints: list[object] = field(default_factory=list)

    def append_many(self, batches: list[tuple[object, list[dict]]], checkpoint: Callable[[object], None]) -> None:
        self.batches.append(batches)
        self.checkpoints.append(checkpoint("db"))


def test_metrics_spools_every_named_table_in_one_batch() -> None:
    box = _Box()
    tables = {"passes": "passes table", "stages": "stages table"}
    port = metrics.SpoolMetrics(cast(Any, box), cast(Any, tables))
    assert isinstance(port, ports.Metrics)
    rows = {"passes": ({"a": 1}, {"a": 2}), "stages": [{"b": 3}]}
    assert port.emit(rows, deadline=_open()) == 3
    assert box.batches == [[("passes table", [{"a": 1}, {"a": 2}]), ("stages table", [{"b": 3}])]]
    assert box.checkpoints == [None]


def test_metrics_refuses_an_unknown_table_and_an_expired_deadline() -> None:
    box = _Box()
    port = metrics.SpoolMetrics(cast(Any, box), cast(Any, {"passes": "passes table"}))
    with pytest.raises(KeyError, match="unknown metrics table: other"):
        port.emit({"other": [{}]}, deadline=_open())
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        port.emit({"passes": [{}]}, deadline=_expired())
    assert box.batches == []


def test_host_reads_and_runs_named_jobs() -> None:
    ran: list[Mapping[str, object]] = []
    port = host.JobHost({"sample": lambda: "sample"}, {"refresh": lambda request: ran.append(request) or "started"})
    assert isinstance(port, ports.Host)
    assert port.read("sample", deadline=_open()) == "sample"
    assert port.run({"job": "refresh"}, deadline=_open()) == "started"
    assert ran == [{"job": "refresh"}]
    assert port.read("disk", deadline=_open()) == ports.Unsupported("disk")
    assert port.run({"job": "build"}, deadline=_open()) == ports.Unsupported("build")
    assert port.run({}, deadline=_open()) == ports.Unsupported("")
    assert port.run({"job": 7}, deadline=_open()) == ports.Unsupported("7")


@pytest.mark.parametrize("operation,arg", [("read", "sample"), ("run", {"job": "refresh"})])
def test_host_refuses_an_expired_deadline_before_the_job(operation: str, arg: object) -> None:
    calls: list[object] = []
    port = host.JobHost({"sample": lambda: calls.append("read")}, {"refresh": lambda request: calls.append(request)})
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        getattr(port, operation)(arg, deadline=_expired())
    assert calls == []


def test_local_host_wires_the_controller_sample_and_both_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import host_budget, refresh_job, snapshot_job

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(host_budget, "controller_host", lambda environ: calls.append(("sample", environ)) or "s")
    monkeypatch.setattr(refresh_job, "start", lambda store, slug: calls.append(("refresh", store, slug)) or "r")
    monkeypatch.setattr(
        snapshot_job,
        "start",
        lambda store, slug, now_ms, environ: calls.append(("snapshot", store, slug, now_ms, environ)) or "n",
    )
    environ = {"A": "1"}
    port = host.local_host(cast(Any, "store"), "slug", environ)
    assert port.read("sample", deadline=_open()) == "s"
    assert port.run({"job": "refresh"}, deadline=_open()) == "r"
    assert port.run({"job": "snapshot", "now_ms": 5}, deadline=_open()) == "n"
    assert calls == [
        ("sample", environ),
        ("refresh", "store", "slug"),
        ("snapshot", "store", "slug", 5, environ),
    ]


def _banned_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level:
            found.append(f"relative import {'.' * node.level}{node.module or ''}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        found += [name for name in names if any(name == ban or name.startswith(f"{ban}.") for ban in BANNED)]
    return found


def test_the_import_check_catches_a_planted_direct_import() -> None:
    assert _banned_imports("from scripts.swarm import refresh_job\nimport hooks.classifier.api\n") == [
        "scripts.swarm.refresh_job",
        "hooks.classifier.api",
    ]
    assert _banned_imports("from scripts.inbox.store import InboxStore\n") == [
        "scripts.inbox.store",
        "scripts.inbox.store.InboxStore",
    ]
    assert _banned_imports("from ..swarm import refresh_job\nfrom .. import inbox\n") == [
        "relative import ..swarm",
        "relative import ..",
    ]
    assert _banned_imports("from scripts.tick.ports import Tools\nimport scripts.inboxes\n") == []


def test_no_tick_step_module_imports_an_adapted_implementation() -> None:
    modules = [path for path in TICK.rglob("*.py") if "adapters" not in path.relative_to(TICK).parts]
    assert modules
    offenders = {str(path.relative_to(ROOT)): _banned_imports(path.read_text()) for path in modules}
    assert {path: names for path, names in offenders.items() if names} == {}
