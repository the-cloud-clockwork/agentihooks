from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from hooks.proc import Process
from scripts.swarm import reaper
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status
from scripts.tick.adapters import processes as process_adapter
from scripts.tick.adapters import runtime as runtime_adapter
from scripts.tick.deadline import Deadline


def _process(pid: int, pgid: int, argv: tuple[str, ...] = ("claude",)) -> Process:
    return Process(pid=pid, ppid=1, pgid=pgid, sid=pgid, start_time=pid * 10, state="S", comm="claude", argv=argv)


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def observe(self, agent: AgentRecord) -> Outcome:
        self.calls.append(("observe", agent, None))
        return Outcome("observe", Status.OK, "local", value=agent.name)

    def spawn(self, request: SpawnRequest) -> Outcome:
        self.calls.append(("spawn", request, None))
        return Outcome("spawn", Status.OK, "local", value=request.name)

    def terminate(self, agent: AgentRecord, homes: tuple[Path, ...] = ()) -> Outcome:
        self.calls.append(("terminate", agent, homes))
        return Outcome("terminate", Status.OK, "local", value=agent.name)


def _runtime(
    agents: dict[str, AgentRecord], herdr: Callable[[list[str]], dict[str, object]] | None = None
) -> tuple[runtime_adapter.RouterRuntime, _Router]:
    router = _Router()
    homes = {"eng-a": (Path("/scratch/a"),)}
    adapter = runtime_adapter.RouterRuntime(
        router,
        agents.get,
        herdr=herdr or (lambda args: {"panes": [{"pane_id": "w1:p1", "args": args}]}),
        homes=lambda agent: homes.get(agent.name, ()),
    )
    return adapter, router


def test_one_scan_serves_every_step_of_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[int] = []
    table = {10: _process(10, 10), 11: _process(11, 10)}

    def scan() -> dict[int, Process]:
        scans.append(1)
        return table

    shared = process_adapter.PassProcesses(scan)
    deadline = Deadline.after(10)
    monkeypatch.setattr(reaper, "processes", lambda: pytest.fail("a step scanned processes itself"))
    monkeypatch.setattr(reaper, "carries", lambda process, name: name == "eng-a")
    first = shared.scan(deadline=deadline)
    second = shared.scan(deadline=deadline)
    found = reaper.targets("eng-a", 10, [], table=second)
    assert first is second is table
    assert scans == [1]
    assert shared.scans == 1
    assert found == reaper.Targets(frozenset([10]), frozenset())


def test_each_pass_scans_afresh() -> None:
    tables = [{1: _process(1, 1)}, {2: _process(2, 2)}]
    first = process_adapter.PassProcesses(lambda: tables[0])
    second = process_adapter.PassProcesses(lambda: tables[1])
    deadline = Deadline.after(10)
    assert first.scan(deadline=deadline) == tables[0]
    assert second.scan(deadline=deadline) == tables[1]


def test_expired_deadline_refuses_scan_before_reading_processes() -> None:
    shared = process_adapter.PassProcesses(lambda: pytest.fail("scanned after the deadline"))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        shared.scan(deadline=Deadline(0.0, clock=lambda: 1.0))
    assert shared.scans == 0


def test_reap_selects_from_the_shared_table(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {20: _process(20, 20), 21: _process(21, 21)}
    ended: list[reaper.Targets] = []
    monkeypatch.setattr(reaper, "processes", lambda: pytest.fail("reap scanned processes itself"))
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))
    reaper.reap([20, 99], table=table)
    assert ended == [reaper.Targets(frozenset([20]), frozenset())]


def test_retire_selects_from_the_shared_table(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {30: _process(30, 30, ("claude", "--name", "eng-b"))}
    ended: list[reaper.Targets] = []
    monkeypatch.setattr(reaper, "processes", lambda: pytest.fail("retire scanned processes itself"))
    monkeypatch.setattr(reaper, "carries", lambda process, name: name == "eng-b")
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))
    reaper.retire("eng-b", 30, [], 300, table=table)
    assert ended == [reaper.Targets(frozenset([30]), frozenset())]


def test_inspect_observes_the_named_agent_through_the_router() -> None:
    agent = AgentRecord(name="eng-a", lane="eng", task="t1")
    adapter, router = _runtime({"eng-a": agent})
    outcome = adapter.inspect("eng-a", deadline=Deadline.after(10))
    assert outcome == Outcome("observe", Status.OK, "local", value="eng-a")
    assert router.calls == [("observe", agent, None)]


def test_unknown_handle_is_unavailable_without_a_router_call() -> None:
    adapter, router = _runtime({})
    for operation in ("inspect", "stop"):
        outcome = getattr(adapter, operation)("ghost", deadline=Deadline.after(10))
        assert outcome == Outcome(operation, Status.UNAVAILABLE, "", detail="no agent named ghost")
    assert router.calls == []


def test_launch_spawns_the_request_through_the_router() -> None:
    adapter, router = _runtime({})
    request = {"config": None, "lane": "eng", "name": "eng-c", "task": {"id": "t2"}}
    outcome = adapter.launch(request, deadline=Deadline.after(10))
    assert outcome == Outcome("spawn", Status.OK, "local", value="eng-c")
    assert router.calls == [("spawn", SpawnRequest(None, "eng", "eng-c", {"id": "t2"}), None)]


def test_stop_terminates_with_the_agent_scratch_homes() -> None:
    agent = AgentRecord(name="eng-a", lane="eng", task="t1")
    adapter, router = _runtime({"eng-a": agent})
    adapter.stop("eng-a", deadline=Deadline.after(10))
    assert router.calls == [("terminate", agent, (Path("/scratch/a"),))]


def test_panes_lists_herdr_panes() -> None:
    calls: list[list[str]] = []

    def herdr(args: list[str]) -> dict[str, object]:
        calls.append(args)
        return {"panes": [{"pane_id": "w1:p1"}]}

    adapter, _ = _runtime({}, herdr)
    assert adapter.panes(deadline=Deadline.after(10)) == [{"pane_id": "w1:p1"}]
    assert calls == [["pane", "list"]]


def test_panes_without_a_list_answer_is_empty() -> None:
    adapter, _ = _runtime({}, lambda args: {})
    assert adapter.panes(deadline=Deadline.after(10)) == []


@pytest.mark.parametrize(
    "operation,argument",
    [("inspect", "eng-a"), ("launch", {"config": None, "lane": "eng", "name": "x", "task": {}}), ("stop", "eng-a")],
)
def test_expired_deadline_refuses_runtime_operation_before_router(operation: str, argument: object) -> None:
    adapter, router = _runtime({"eng-a": AgentRecord(name="eng-a", lane="eng", task="t1")})
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        getattr(adapter, operation)(argument, deadline=Deadline(0.0, clock=lambda: 1.0))
    assert router.calls == []


def test_expired_deadline_refuses_pane_list_before_herdr() -> None:
    adapter, _ = _runtime({}, lambda args: pytest.fail("listed panes after the deadline"))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        adapter.panes(deadline=Deadline(0.0, clock=lambda: 1.0))


def test_a_tick_pass_scans_processes_once_for_every_retire_and_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {40: _process(40, 40), 41: _process(41, 41)}
    scans: list[int] = []
    ended: list[reaper.Targets] = []

    def scan() -> dict[int, Process]:
        scans.append(1)
        return table

    monkeypatch.setattr(reaper, "processes", scan)
    monkeypatch.setattr(reaper, "carries", lambda process, name: True)
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))

    @reaper.scanning_pass
    def tick_pass() -> None:
        reaper.retire("eng-a", 40, [], 400)
        reaper.retire("eng-b", 41, [], 410)
        reaper.reap([40, 41])

    tick_pass()
    assert scans == [1]
    assert ended == [
        reaper.Targets(frozenset([40]), frozenset()),
        reaper.Targets(frozenset([41]), frozenset()),
        reaper.Targets(frozenset([40, 41]), frozenset()),
    ]
    assert reaper.PASS.get() is None


def test_each_tick_pass_takes_its_own_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[int] = []
    monkeypatch.setattr(reaper, "processes", lambda: scans.append(1) or {})
    monkeypatch.setattr(reaper, "end", lambda found: reaper.Outcome(()))
    tick_pass = reaper.scanning_pass(lambda: reaper.reap([]))
    tick_pass()
    tick_pass()
    assert scans == [1, 1]


def test_outside_a_pass_every_selection_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[int] = []
    monkeypatch.setattr(reaper, "processes", lambda: scans.append(1) or {})
    monkeypatch.setattr(reaper, "end", lambda found: reaper.Outcome(()))
    reaper.reap([])
    reaper.reap([])
    assert scans == [1, 1]


def test_a_process_newer_than_the_pass_snapshot_rescans(monkeypatch: pytest.MonkeyPatch) -> None:
    tables = [{50: _process(50, 50)}, {50: _process(50, 50), 51: _process(51, 51)}]
    scans: list[int] = []
    ended: list[reaper.Targets] = []

    def scan() -> dict[int, Process]:
        scans.append(1)
        return tables[len(scans) - 1]

    monkeypatch.setattr(reaper, "processes", scan)
    monkeypatch.setattr(reaper, "carries", lambda process, name: True)
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))

    @reaper.scanning_pass
    def tick_pass() -> None:
        reaper.reap([50])
        reaper.retire("eng-new", 51, [], 510)

    tick_pass()
    assert scans == [1, 1]
    assert ended == [reaper.Targets(frozenset([50]), frozenset()), reaper.Targets(frozenset([51]), frozenset())]


def test_the_tick_entry_runs_inside_one_scan_pass() -> None:
    from scripts.swarm import tick

    assert tick.tick.__wrapped__.__name__ == "tick"
    assert tick.tick.__qualname__ == "tick"
    assert tick.tick.__code__ is reaper.scanning_pass(lambda: None).__code__


def test_liveness_reads_each_member_without_a_table_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    read: list[int] = []
    alive = {60: _process(60, 60), 62: Process(62, 1, 62, 62, 620, "Z", "claude", ())}

    def one(pid: int, proc: Path) -> Process | None:
        read.append(pid)
        assert proc == Path("/proc")
        return alive.get(pid)

    monkeypatch.setattr(reaper, "processes", lambda: pytest.fail("liveness scanned the whole table"))
    monkeypatch.setattr(reaper, "_process", one)
    assert reaper._alive({60: 600, 61: 610, 62: 620, 63: 1}) == [60]
    assert sorted(read) == [60, 61, 62, 63]
