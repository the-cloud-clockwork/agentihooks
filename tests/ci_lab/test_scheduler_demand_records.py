import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from scripts.ci_lab import scheduler
from tests.ci_lab.test_scheduler import HEAD_A, Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab


@pytest.fixture
def grading(lab: Callable[..., Run]) -> Run:
    run = lab({HEAD_A: plan({"lint": (unit(1), unit(2))})})
    run.cache(unit(2))
    run.push(HEAD_A)
    run.tick(1000)
    return run


def stored(run: Run, name: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
    client = run.lab.client
    record = client.hget(scheduler.RECORDS, name)
    revision = client.hget(scheduler.REVISIONS, name)
    return client.get(scheduler.GRADES + name), revision, None if record is None else json.loads(record)


@pytest.mark.feature("lab-scheduler-grade-demand-records")
def test_a_save_stores_the_document_with_its_next_revision_and_the_demand_record_of_that_revision(
    grading: Run,
) -> None:
    state = grading.grade(HEAD_A)
    _, before, _ = stored(grading, state.run)
    grading.engine.save(replace(state, tier=1))
    document, revision, record = stored(grading, state.run)
    assert before is not None
    assert revision == str(int(before) + 1)
    assert record == {"revision": int(before) + 1, "tier": 1, "hits": 1, "jobs": [scheduler.job_id(HEAD_A, unit(1))]}
    assert document is not None
    assert record is not None
    demand = scheduler._grade_demand(document, b"")
    assert (record["tier"], record["hits"], tuple(record["jobs"])) == (demand.tier, demand.hits, demand.jobs)


@pytest.mark.feature("lab-scheduler-grade-demand-records")
def test_the_first_save_of_a_grade_without_a_revision_records_revision_one(grading: Run) -> None:
    state = grading.grade(HEAD_A)
    grading.lab.client.hdel(scheduler.REVISIONS, state.run)
    grading.engine.save(state)
    _, revision, record = stored(grading, state.run)
    assert revision == "1"
    assert record is not None
    assert record["revision"] == 1


@pytest.mark.feature("lab-scheduler-grade-demand-records")
def test_a_failed_save_transaction_leaves_the_document_revision_and_record_as_they_were(
    grading: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = grading.grade(HEAD_A)
    before = stored(grading, state.run)
    client = grading.lab.client
    pipeline = client.pipeline

    def refused() -> list[Any]:
        raise RedisConnectionError("Connection reset by peer")

    def failing(*args: Any, **options: Any) -> Any:
        pipe = pipeline(*args, **options)
        pipe.execute = refused
        return pipe

    monkeypatch.setattr(client, "pipeline", failing)
    with pytest.raises(RedisConnectionError):
        grading.engine.save(replace(state, tier=1))
    monkeypatch.undo()
    assert before[1] is not None
    assert stored(grading, state.run) == before


@pytest.mark.feature("lab-scheduler-grade-demand-records")
def test_a_save_racing_another_writer_of_the_grade_retries_and_records_the_revision_it_stored(
    grading: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = grading.grade(HEAD_A)
    client = grading.lab.client
    name = scheduler.GRADES + state.run
    _, before, _ = stored(grading, state.run)
    pipeline = client.pipeline
    raced: list[str] = []

    def racing(*args: Any, **options: Any) -> Any:
        pipe = pipeline(*args, **options)
        multi = pipe.multi

        def writing() -> None:
            if not raced:
                raced.append(state.run)
                client.hincrby(scheduler.REVISIONS, state.run, 1)
                client.set(name, json.dumps({**json.loads(client.get(name)), "hold": 1}))
            multi()

        pipe.multi = writing
        return pipe

    monkeypatch.setattr(client, "pipeline", racing)
    grading.engine.save(replace(state, tier=1))
    monkeypatch.undo()
    document, revision, record = stored(grading, state.run)
    assert raced == [state.run]
    assert before is not None
    assert revision == str(int(before) + 2)
    assert record is not None
    assert record["revision"] == int(before) + 2
    assert document is not None
    assert json.loads(document)["tier"] == 1


@pytest.mark.feature("lab-scheduler-grade-demand-records")
def test_a_completed_grade_leaves_the_live_set_with_neither_revision_nor_record_and_a_closed_save_writes_none(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    name = run.grade(HEAD_A).run
    assert stored(run, name)[1] is not None
    run.finish(run.lease()[0], "passed", {"seconds": 2.0})
    actions = [*run.tick(1100), *run.tick(1200)]
    closed = run.grade(HEAD_A)
    assert f"completed {name} success" in actions
    assert stored(run, name)[1:] == (None, None)
    run.engine.save(closed)
    document, revision, record = stored(run, name)
    assert (revision, record) == (None, None)
    assert document is not None
    assert json.loads(document)["conclusion"] == "success"


@pytest.mark.feature("lab-scheduler-grade-demand-records")
def test_a_forgotten_grade_leaves_the_live_set_with_neither_revision_nor_record(grading: Run) -> None:
    name = grading.grade(HEAD_A).run
    assert stored(grading, name)[1] is not None
    grading.lab.client.delete(scheduler.GRADES + name)
    assert f"forgot {name}" in grading.tick(1100)
    assert stored(grading, name) == (None, None, None)
    assert not grading.lab.client.sismember(scheduler.LIVE, name)
