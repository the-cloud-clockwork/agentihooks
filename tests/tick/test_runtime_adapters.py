from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from hooks.proc import Process
from scripts.swarm import reaper
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, SpawnRequest, Status
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
        cast(RuntimeRouter, router),
        agents.get,
        herdr=herdr or (lambda args: {"panes": [{"pane_id": "w1:p1", "args": args}]}),
        homes=lambda agent: homes.get(agent.name, ()),
    )
    return adapter, router


def test_one_scan_serves_every_read_of_a_pass() -> None:
    scans: list[int] = []
    table = {10: _process(10, 10), 11: _process(11, 10)}

    def scan() -> dict[int, Process]:
        scans.append(1)
        return table

    shared = process_adapter.PassProcesses(scan)
    deadline = Deadline.after(10)
    first = shared.scan(deadline=deadline)
    second = shared.scan(deadline=deadline)
    assert first is second is table
    assert scans == [1]


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


def test_expired_deadline_refuses_rescan_before_reading_processes() -> None:
    shared = process_adapter.PassProcesses(lambda: pytest.fail("rescanned after the deadline"))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        shared.rescan(deadline=Deadline(0.0, clock=lambda: 1.0))


def test_the_pass_wrapper_forwards_positional_and_keyword_arguments() -> None:
    def step(first: int, second: int, *, third: int) -> tuple[int, int, int, bool]:
        return first, second, third, reaper.PASS.get() is not None

    assert reaper.scanning_pass(step)(1, 2, third=3) == (1, 2, 3, True)
    assert reaper.PASS.get() is None


def test_a_live_process_missing_from_the_snapshot_is_read_from_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    own = os.getpid()
    tables: list[dict[int, Process]] = [{}, {own: _process(own, own)}]
    scans: list[int] = []
    ended: list[reaper.Targets] = []

    def scan() -> dict[int, Process]:
        scans.append(1)
        return tables[len(scans) - 1]

    monkeypatch.setattr(reaper, "processes", scan)
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))
    reaper.scanning_pass(lambda: reaper.reap([own]))()
    assert scans == [1, 1]
    assert len(ended) == 1


def test_herdr_default_calls_the_swarm_herdr_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import runtime as swarm_runtime

    calls: list[list[str]] = []
    monkeypatch.setattr(swarm_runtime, "herdr_call", lambda args: calls.append(args) or {"panes": [{"pane_id": "p"}]})
    adapter = runtime_adapter.RouterRuntime(cast(RuntimeRouter, _Router()), {}.get)
    assert adapter.panes(deadline=Deadline.after(10)) == [{"pane_id": "p"}]
    assert calls == [["pane", "list"]]


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


def test_an_exited_process_missing_from_the_snapshot_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[int] = []
    ended: list[reaper.Targets] = []
    monkeypatch.setattr(reaper, "processes", lambda: scans.append(1) or {70: _process(70, 70)})
    monkeypatch.setattr(reaper, "_process", lambda pid, proc: None)
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))

    @reaper.scanning_pass
    def tick_pass() -> None:
        reaper.reap([70, 71])
        reaper.reap([72])

    tick_pass()
    assert scans == [1]
    assert ended == [reaper.Targets(frozenset([70]), frozenset()), reaper.Targets()]


def test_a_live_process_newer_than_the_pass_snapshot_rescans(monkeypatch: pytest.MonkeyPatch) -> None:
    tables = [{50: _process(50, 50)}, {50: _process(50, 50), 51: _process(51, 51)}]
    scans: list[int] = []
    ended: list[reaper.Targets] = []

    def scan() -> dict[int, Process]:
        scans.append(1)
        return tables[len(scans) - 1]

    monkeypatch.setattr(reaper, "processes", scan)
    monkeypatch.setattr(reaper, "_process", lambda pid, proc: tables[1].get(pid))
    monkeypatch.setattr(reaper, "carries", lambda process, name: True)
    monkeypatch.setattr(reaper, "end", lambda found: ended.append(found) or reaper.Outcome(()))

    @reaper.scanning_pass
    def tick_pass() -> None:
        reaper.reap([50])
        reaper.retire("eng-new", 51, [], 510)

    tick_pass()
    assert scans == [1, 1]
    assert ended == [reaper.Targets(frozenset([50]), frozenset()), reaper.Targets(frozenset([51]), frozenset())]


def test_the_tick_entry_carries_the_scan_pass_wrapper() -> None:
    from scripts.swarm import tick

    assert getattr(tick.tick, "__wrapped__").__name__ == "tick"
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


def test_rescan_replaces_the_pass_snapshot() -> None:
    tables = [{1: _process(1, 1)}, {2: _process(2, 2)}]
    scans: list[int] = []
    shared = process_adapter.PassProcesses(lambda: scans.append(1) or tables[len(scans) - 1])
    deadline = Deadline.after(10)
    assert shared.scan(deadline=deadline) == tables[0]
    assert shared.rescan(deadline=deadline) == tables[1]
    assert shared.scan(deadline=deadline) == tables[1]
    assert scans == [1, 1]


def test_real_termination_in_a_pass_reads_membership_fresh_once_per_end(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {80: _process(80, 80), 81: _process(81, 80), 90: _process(90, 90)}
    scans: list[int] = []
    sent: list[tuple[str, int, int]] = []
    monkeypatch.setattr(reaper, "processes", lambda: scans.append(1) or table)
    monkeypatch.setattr(reaper, "carries", lambda process, name: True)
    monkeypatch.setattr(reaper, "_process", lambda pid, proc: None)
    monkeypatch.setattr(reaper.os, "killpg", lambda pgid, sig: sent.append(("group", pgid, sig)))
    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: sent.append(("single", pid, sig)))

    @reaper.scanning_pass
    def tick_pass() -> list[reaper.Outcome]:
        return [reaper.retire("eng-a", 80, [], 800), reaper.reap([90])]

    assert tick_pass() == [reaper.Outcome((80, 81)), reaper.Outcome((90,))]
    assert scans == [1, 1, 1]
    assert sent == [("group", 80, reaper.signal.SIGTERM), ("group", 90, reaper.signal.SIGTERM)]


def test_a_rescanned_process_is_waited_on_from_the_new_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    tables = [{}, {95: _process(95, 95)}, {95: _process(95, 95)}]
    scans: list[int] = []

    def scan() -> dict[int, Process]:
        scans.append(1)
        return tables[len(scans) - 1]

    monkeypatch.setattr(reaper, "processes", scan)
    monkeypatch.setattr(reaper, "carries", lambda process, name: True)
    monkeypatch.setattr(reaper, "_process", lambda pid, proc: tables[1][pid] if len(scans) == 1 else None)
    monkeypatch.setattr(reaper.os, "killpg", lambda pgid, sig: None)

    @reaper.scanning_pass
    def tick_pass() -> reaper.Outcome:
        return reaper.retire("eng-new", 95, [], 950)

    assert tick_pass() == reaper.Outcome((95,))
    assert scans == [1, 1, 1]


def test_a_member_joining_after_selection_is_waited_on(monkeypatch: pytest.MonkeyPatch) -> None:
    tables = [{100: _process(100, 100)}, {100: _process(100, 100), 101: _process(101, 100)}]
    scans: list[int] = []

    def scan() -> dict[int, Process]:
        scans.append(1)
        return tables[min(len(scans), 2) - 1]

    monkeypatch.setattr(reaper, "processes", scan)
    monkeypatch.setattr(reaper, "carries", lambda process, name: True)
    monkeypatch.setattr(reaper, "_process", lambda pid, proc: None)
    monkeypatch.setattr(reaper.os, "killpg", lambda pgid, sig: None)

    @reaper.scanning_pass
    def tick_pass() -> reaper.Outcome:
        return reaper.retire("eng-a", 100, [], 1000)

    assert tick_pass() == reaper.Outcome((100, 101))
