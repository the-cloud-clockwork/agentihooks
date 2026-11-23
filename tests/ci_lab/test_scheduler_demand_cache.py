from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.retry import Policy
from tests.ci_lab.test_scheduler import HEAD_A, HEAD_B, Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab


@pytest.fixture
def grades(lab: Callable[..., Run]) -> Run:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    run.push(HEAD_A, lane="first")
    run.push(HEAD_B, lane="second")
    run.tick(1000)
    return run


def _parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    parse = scheduler._parse

    def counted(raw: str | bytes) -> scheduler.State:
        state = parse(raw)
        calls.append(state.run)
        return state

    monkeypatch.setattr(scheduler, "_parse", counted)
    return calls


def _fetched(client: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Record the runs whose demand record and whose grade document each command the client sends, directly or through
    a pipeline, asks for."""
    records: list[str] = []
    documents: list[str] = []
    command, pipeline = client.execute_command, client.pipeline

    def note(args: tuple[Any, ...]) -> None:
        name, key = str(args[0]).upper(), str(args[1])
        if name == "HMGET" and key == scheduler.RECORDS:
            records.extend(str(run) for run in args[2:])
        if name == "GET" and key.startswith(scheduler.GRADES):
            documents.append(key.removeprefix(scheduler.GRADES))

    def direct(*args: Any, **options: Any) -> Any:
        note(args)
        return command(*args, **options)

    def piped(**options: Any) -> Any:
        pipe = pipeline(**options)
        stack = pipe.execute_command

        def stacked(*args: Any, **kwargs: Any) -> Any:
            note(args)
            return stack(*args, **kwargs)

        pipe.execute_command = stacked
        return pipe

    monkeypatch.setattr(client, "execute_command", direct)
    monkeypatch.setattr(client, "pipeline", piped)
    return records, documents


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_an_unchanged_load_equals_a_fresh_build_without_parsing_grades(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = grades.engine.load(1000)
    fresh = grades.lab.build(0, Policy(), None).load(1000)
    calls = _parses(monkeypatch)

    assert grades.engine.load(1000) == first == fresh
    assert first.cache_hits == {}
    assert first.cache_misses == {"promotion": 2}
    assert first.pending_slots == {"promotion": 2}
    assert calls == []


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_saved_grade_is_reparsed_and_changes_the_next_load_demand(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = grades.grade(HEAD_A)
    first = grades.engine.load(1000)
    calls = _parses(monkeypatch)
    changed.tier = 1
    grades.engine.save(changed)

    updated = grades.engine.load(1000)
    assert calls == [changed.run]
    assert updated.cache_misses == {"promotion": 1, "pull_request": 1}
    assert updated.pending_slots == {"promotion": 1, "pull_request": 1}
    assert first.cache_misses == {"promotion": 2}
    calls.clear()
    grades.engine.save(changed)
    assert grades.engine.load(1000) == updated
    assert calls == []


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
@pytest.mark.parametrize("removed", [(HEAD_A,), (HEAD_A, HEAD_B)], ids=["one", "all"])
def test_a_run_leaving_the_live_set_is_parsed_again_if_it_returns(
    grades: Run, monkeypatch: pytest.MonkeyPatch, removed: tuple[str, ...]
) -> None:
    runs = [grades.grade(head).run for head in removed]
    first = grades.engine.load(1000)
    calls = _parses(monkeypatch)
    grades.lab.client.srem(scheduler.LIVE, *runs)

    remaining = grades.engine.load(1000)
    assert remaining.cache_misses == ({"promotion": 1} if len(runs) == 1 else {})
    assert calls == []
    grades.lab.client.sadd(scheduler.LIVE, *runs)
    assert grades.engine.load(1000) == first
    assert calls == sorted(runs)
    calls.clear()
    assert grades.engine.load(1000) == first
    assert calls == []


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_missing_grade_stops_contributing_and_is_parsed_when_restored(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = {state.run: state for state in (grades.grade(HEAD_A), grades.grade(HEAD_B))}
    missing = states[min(states)]
    first = grades.engine.load(1000)
    calls = _parses(monkeypatch)
    grades.lab.client.delete(scheduler.GRADES + missing.run)

    remaining = grades.engine.load(1000)
    assert remaining.cache_misses == {"promotion": 1}
    assert remaining.pending_slots == {"promotion": 1}
    assert calls == []
    grades.engine.save(missing)
    assert grades.engine.load(1000) == first
    assert calls == [missing.run]


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_cached_contributions_preserve_duplicate_slots_and_the_most_urgent_shared_job(grades: Run) -> None:
    urgent = grades.grade(HEAD_A)
    other = grades.grade(HEAD_B)
    urgent.tier = 1
    urgent_slot = next(iter(urgent.units.values()))
    other_slot = next(iter(other.units.values()))
    other.units = {
        "shared": replace(other_slot, job=urgent_slot.job),
        "duplicate": replace(other_slot, job=urgent_slot.job),
        "cached": replace(other_slot, job=""),
    }
    grades.engine.save(urgent)
    grades.engine.save(other)

    first = grades.engine.load(1000)
    assert first.cache_hits == {"promotion": 1}
    assert first.cache_misses == {"promotion": 2, "pull_request": 1}
    assert first.pending_slots == {"pull_request": 1}
    assert grades.engine.load(1000) == first
    assert grades.lab.build(0, Policy(), None).load(1000) == first

    urgent_slot.job = ""
    grades.engine.save(urgent)
    updated = grades.engine.load(1000)
    assert updated.cache_hits == {"promotion": 1, "pull_request": 1}
    assert updated.cache_misses == {"promotion": 2}
    assert updated.pending_slots == {"promotion": 1}
    assert grades.engine.load(1000) == updated
    assert grades.lab.build(0, Policy(), None).load(1000) == updated
    assert first.cache_misses == {"promotion": 2, "pull_request": 1}

    urgent.tier = other.tier
    grades.engine.save(urgent)
    combined = grades.engine.load(1000)
    assert combined.cache_hits == {"promotion": 2}
    assert combined.cache_misses == {"promotion": 2}
    assert grades.engine.load(1000) == combined


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_load_with_demand_records_equals_a_fresh_load_from_the_grade_documents_alone(grades: Run) -> None:
    changed = grades.grade(HEAD_A)
    changed.tier = 1
    grades.engine.save(changed)
    first = grades.engine.load(1000)
    grades.lab.client.delete(scheduler.REVISIONS, scheduler.RECORDS)

    assert grades.lab.build(0, Policy(), None).load(1000) == first
    assert first.cache_misses == {"promotion": 1, "pull_request": 1}


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_load_after_an_unchanged_revision_reads_no_record_and_no_grade_document(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = grades.engine.load(1000)
    records, documents = _fetched(grades.lab.client, monkeypatch)

    assert grades.engine.load(1000) == first
    assert (records, documents) == ([], [])


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_moved_revision_reads_only_that_run_s_record_and_no_grade_document(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = grades.grade(HEAD_A)
    grades.engine.load(1000)
    records, documents = _fetched(grades.lab.client, monkeypatch)
    changed.tier = 1
    grades.engine.save(changed)

    updated = grades.engine.load(1000)
    assert (records, documents) == ([changed.run], [])
    assert updated.cache_misses == {"promotion": 1, "pull_request": 1}
    assert updated == grades.lab.build(0, Policy(), None).load(1000)


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_live_run_without_a_record_reads_its_grade_document_on_every_build_until_a_save_records_it(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = grades.engine.load(1000)
    old = grades.grade(HEAD_A)
    grades.lab.client.hdel(scheduler.REVISIONS, old.run)
    grades.lab.client.hdel(scheduler.RECORDS, old.run)
    records, documents = _fetched(grades.lab.client, monkeypatch)

    assert grades.engine.load(1000) == first
    assert grades.engine.load(1000) == first
    assert (records, documents) == ([old.run, old.run], [old.run, old.run])
    old.tier = 1
    grades.engine.save(old)
    records.clear()
    documents.clear()
    updated = grades.engine.load(1000)
    assert (records, documents) == ([old.run], [])
    assert updated.cache_misses == {"promotion": 1, "pull_request": 1}


@pytest.mark.feature("lab-scheduler-grade-demand-cache")
def test_a_cold_build_reads_every_live_run_s_record_and_no_grade_document_when_each_has_a_record(
    grades: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = sorted(grades.grade(head).run for head in (HEAD_A, HEAD_B))
    first = grades.engine.load(1000)
    cold = grades.lab.build(0, Policy(), None)
    records, documents = _fetched(grades.lab.client, monkeypatch)

    assert cold.load(1000) == first
    assert (records, documents) == (runs, [])
