from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci_lab import queue, scheduler
from tests.ci_lab.test_scheduler import HEAD_A, HEAD_B, Run, plan, ref, unit
from tests.ci_lab.test_scheduler import lab as lab

JOB_KEYS = f"{queue.PREFIX}job:"


def pipelines(client: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any], list[Any]]]:
    """Record the arguments and the queued commands of every pipeline the client executes, in order."""
    pipeline = client.pipeline
    sent: list[tuple[tuple[Any, ...], dict[str, Any], list[Any]]] = []

    def piped(*args: Any, **options: Any) -> Any:
        pipe = pipeline(*args, **options)
        execute = pipe.execute

        def executed(*more: Any, **kwargs: Any) -> Any:
            sent.append((args, options, [command for command, _ in pipe.command_stack]))
            return execute(*more, **kwargs)

        monkeypatch.setattr(pipe, "execute", executed)
        return pipe

    monkeypatch.setattr(client, "pipeline", piped)
    return sent


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
    run.lab.client.delete(f"{JOB_KEYS}{scheduler.job_id(HEAD_A, units[0])}")
    run.lab.client.delete(f"{JOB_KEYS}{scheduler.job_id(HEAD_A, units[2])}")
    client = run.lab.client
    command = client.execute_command
    direct_reads: list[tuple[Any, ...]] = []

    def direct(*args: Any, **options: Any) -> Any:
        if args[0] == "HGETALL" and str(args[1]).startswith(JOB_KEYS):
            direct_reads.append(args)
        return command(*args, **options)

    monkeypatch.setattr(client, "execute_command", direct)
    sent = pipelines(client, monkeypatch)

    run.tick(1100)

    batches = [
        (args, options, [read[1] for read in reads if read[0] == "HGETALL" and str(read[1]).startswith(JOB_KEYS)])
        for args, options, reads in sent
    ]
    batches = [batch for batch in batches if batch[2]]
    assert direct_reads == []
    assert [len(keys) for _, _, keys in batches] == [500, 500, 3]
    assert all((args, options) == ((), {"transaction": False}) for args, options, _ in batches)
    assert [key for _, _, keys in batches for key in keys] == [
        f"{JOB_KEYS}{scheduler.job_id(HEAD_A, item)}" for item in units[2:]
    ]
    slots = run.grade(HEAD_A).units
    assert [slots[ref(item)].outcome for item in units[:3]] == ["passed", "", "dropped"]
    assert all(slot.held and not slot.outcome for slot in run.grade(HEAD_B).units.values())
