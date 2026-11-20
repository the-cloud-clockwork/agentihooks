import weakref
from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci_lab import queue, scheduler
from tests.ci_lab.test_scheduler import HEAD_A, LOW_BUDGET, Run, _spend, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab


@pytest.mark.feature("lab-scheduler-memory")
def test_a_pass_and_a_load_hold_one_parsed_live_grade_at_a_time_however_many_grades_hold_units(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    heads = [f"{number:x}" * 40 for number in range(1, 6)]
    run = lab({head: plan({"lint": (unit(number),)}) for number, head in enumerate(heads, 1)}, spend=LOW_BUDGET)
    _spend(run)
    for head in heads:
        run.push(head, lane=f"sweep-{head[0]}", event="backfill")
    run.tick(1000)
    assert [slot.held for grade in run.grades() for slot in grade.units.values()] == [True] * len(heads)
    alive: list[weakref.ref[scheduler.State]] = []
    sizes: list[int] = []
    parse = scheduler._parse

    def counted(raw: str | bytes) -> scheduler.State:
        state = parse(raw)
        alive.append(weakref.ref(state))
        sizes.append(sum(ref() is not None for ref in alive))
        return state

    monkeypatch.setattr(scheduler, "_parse", counted)
    assert run.tick(1100) == []
    run.engine.load(1100)
    run.engine.grades_of(heads[0])
    assert sizes and max(sizes) <= 2


@pytest.mark.feature("lab-scheduler-load-reads")
def test_a_load_reads_each_queued_job_s_slots_and_lease_start_through_pipelines_and_builds_no_job(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tuple(unit(number) for number in range(1, 101))
    run = lab({HEAD_A: plan({"lint": units})})
    run.push(HEAD_A)
    run.tick(1000)
    assert len(run.lease(slots=4, ttl_ms=10**6)) == 4
    client = run.lab.client
    client.hset(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, units[-1])}", "slots", 3)
    client.delete(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, units[-2])}")
    built: list[queue.Job] = []
    sent: list[tuple[Any, ...]] = []
    opened: list[dict[str, Any]] = []
    init, command, pipeline = queue.Job.__init__, client.execute_command, client.pipeline

    def counted(job: queue.Job, *args: Any, **kwargs: Any) -> None:
        built.append(job)
        init(job, *args, **kwargs)

    def direct(*args: Any, **options: Any) -> Any:
        sent.append(args)
        return command(*args, **options)

    def piped(**options: Any) -> Any:
        opened.append(options)
        return pipeline(**options)

    monkeypatch.setattr(queue.Job, "__init__", counted)
    monkeypatch.setattr(client, "execute_command", direct)
    monkeypatch.setattr(client, "pipeline", piped)
    load = run.engine.load(1001 + int(scheduler.UNIT_ESTIMATE_S * 1000))
    assert built == []
    assert [args for args in sent if any(f"{queue.PREFIX}job:" in str(arg) for arg in args)] == []
    assert opened and all(options == {"transaction": False} for options in opened)
    assert (load.pending_slots, load.slots_busy, load.overdue) == ({"promotion": 97}, 4, 4)
