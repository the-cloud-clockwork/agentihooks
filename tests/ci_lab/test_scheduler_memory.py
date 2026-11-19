import weakref
from collections.abc import Callable

import pytest

from scripts.ci_lab import scheduler
from tests.ci_lab.test_scheduler import LOW_BUDGET, Run, _spend, plan, unit
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
