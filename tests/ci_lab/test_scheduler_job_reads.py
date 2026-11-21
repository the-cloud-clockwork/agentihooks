from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci_lab import queue, scheduler
from tests.ci_lab.test_scheduler import HEAD_A, HEAD_B, Run, plan, ref, unit
from tests.ci_lab.test_scheduler import lab as lab


@pytest.mark.feature("lab-scheduler-job-reads")
def test_a_pass_preserves_collected_missing_empty_and_started_slots(lab: Callable[..., Run]) -> None:
    units = tuple(unit(number) for number in range(1, 6))
    run = lab({HEAD_A: plan({"lint": units})})
    run.push(HEAD_A)
    run.tick(1000)
    grants = run.lease(slots=2, ttl_ms=10**6)
    run.finish(grants[0], "passed", {})
    run.tick(1100)
    state = run.grade(HEAD_A)
    state.units[ref(units[2])].job = ""
    run.engine.save(state)
    run.lab.client.delete(f"{queue.PREFIX}job:{grants[0].job}")
    run.lab.client.delete(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, units[3])}")

    run.tick(1200)

    slots = run.grade(HEAD_A).units
    assert [slots[ref(item)].outcome for item in units] == ["passed", "", "", "dropped", ""]
    assert [slots[ref(item)].started for item in units] == [False, True, False, False, False]


@pytest.mark.feature("lab-scheduler-job-reads")
def test_a_pass_reads_only_open_jobs_in_nontransactional_batches_of_at_most_500(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tuple(unit(number) for number in range(1, 1006))
    held = unit(1006)
    run = lab({HEAD_A: plan({"lint": units}), HEAD_B: plan({"lint": (held,)})})
    run.push(HEAD_A)
    run.push(HEAD_B, lane="sweep", event="backfill")
    run.tick(1000)
    state = run.grade(HEAD_A)
    state.units[ref(units[0])].outcome = "passed"
    state.units[ref(units[1])].job = ""
    run.engine.save(state)
    run.lab.client.delete(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, units[0])}")
    run.lab.client.delete(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, units[2])}")
    client = run.lab.client
    command, pipeline = client.execute_command, client.pipeline
    direct_reads: list[tuple[Any, ...]] = []
    batches: list[tuple[dict[str, Any], list[tuple[Any, ...]]]] = []

    def direct(*args: Any, **options: Any) -> Any:
        if args[0] == "HGETALL":
            direct_reads.append(args)
        return command(*args, **options)

    def piped(**options: Any) -> Any:
        pipe = pipeline(**options)
        execute = pipe.execute

        def executed(*args: Any, **kwargs: Any) -> Any:
            reads = [command for command, _ in pipe.command_stack if command[0] == "HGETALL"]
            if reads:
                batches.append((options, reads))
            return execute(*args, **kwargs)

        monkeypatch.setattr(pipe, "execute", executed)
        return pipe

    monkeypatch.setattr(client, "execute_command", direct)
    monkeypatch.setattr(client, "pipeline", piped)

    run.tick(1100)

    assert direct_reads == []
    assert [len(reads) for _, reads in batches] == [500, 500, 3]
    assert all(options == {"transaction": False} for options, _ in batches)
    assert [args[1] for _, reads in batches for args in reads] == [
        f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, item)}" for item in units[2:]
    ]
    slots = run.grade(HEAD_A).units
    assert [slots[ref(item)].outcome for item in units[:3]] == ["passed", "", "dropped"]
    assert all(slot.held and not slot.outcome for slot in run.grade(HEAD_B).units.values())
