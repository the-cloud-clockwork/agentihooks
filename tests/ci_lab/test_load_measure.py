import json
import time
from collections.abc import Callable
from typing import Any

import pytest
import redis

from scripts.ci_lab import contract, load_measure, scheduler
from tests.ci_lab.test_scheduler import Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab

HEADS = tuple(f"{number:x}" * 40 for number in range(1, 4))
READS = frozenset({"SCARD", "SMEMBERS", "GET", "ZRANGE", "HMGET", "HGETALL"})
URL = "redis://lab"


def _live(lab: Callable[..., Run]) -> Run:
    """Return a lab run with one live grade per head, a worker offer inside the ready window and a stale one."""
    run = lab({head: plan({"lint": (unit(number),)}) for number, head in enumerate(HEADS, 1)})
    for head in HEADS:
        run.push(head, lane=f"sweep-{head[0]}")
    run.tick(1000)
    run.engine.queue.offer("fresh", 4, 1500)
    run.engine.queue.offer("stale", 8, 999)
    return run


def _opened(run: Run, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Answer redis.Redis.from_url for the lab URL with decoded responses with the run's Redis and return the
    environment naming that URL."""
    opened = {(URL, True): run.lab.client}
    monkeypatch.setattr(redis.Redis, "from_url", lambda url, decode_responses: opened[(url, decode_responses)])
    return {contract.REDIS_URL: URL}


@pytest.mark.feature("lab-scheduler-load-measure")
def test_the_measure_prints_three_builds_their_seconds_and_one_parse_per_live_grade_per_build(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _live(lab)
    assert load_measure.main([], _opened(run, monkeypatch)) == 0
    figures = json.loads(capsys.readouterr().out)
    assert sorted(figures) == ["builds", "grades", "leaves_out", "parses", "process_seconds", "wall_seconds"]
    assert (figures["builds"], figures["grades"], figures["parses"]) == (3, len(HEADS), 3 * len(HEADS))
    assert len(figures["process_seconds"]) == len(figures["wall_seconds"]) == 3
    assert min(figures["process_seconds"] + figures["wall_seconds"]) >= 0
    assert figures["leaves_out"] == "the offer prune round trip of each build"


@pytest.mark.feature("lab-scheduler-load-measure")
def test_the_measure_sends_only_read_commands_directly_and_through_pipelines_and_keeps_the_stale_offer(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _live(lab)
    environ = _opened(run, monkeypatch)
    client = run.lab.client
    sent: list[str] = []
    command, pipeline = client.execute_command, client.pipeline

    def direct(*args: Any, **options: Any) -> Any:
        sent.append(str(args[0]).upper())
        return command(*args, **options)

    def piped(**options: Any) -> Any:
        pipe = pipeline(**options)
        stack = pipe.execute_command

        def stacked(*args: Any, **kwargs: Any) -> Any:
            sent.append(str(args[0]).upper())
            return stack(*args, **kwargs)

        pipe.execute_command = stacked
        return pipe

    monkeypatch.setattr(client, "execute_command", direct)
    monkeypatch.setattr(client, "pipeline", piped)
    assert load_measure.main(["--builds", "2"], environ) == 0
    assert json.loads(capsys.readouterr().out)["parses"] == 2 * len(HEADS)
    assert {"SMEMBERS", "GET", "HMGET", "HGETALL"} <= set(sent) <= READS
    assert set(run.engine.queue.offers(0)) == {"fresh", "stale"}


@pytest.mark.feature("lab-scheduler-load-measure")
def test_a_measured_build_returns_the_load_a_plain_build_returns_with_offers_on_both_sides_of_the_ready_cut(
    lab: Callable[..., Run],
) -> None:
    run = _live(lab)
    now_ms = 1000 + scheduler.READY_MS
    kept = load_measure.reader(run.lab.client)
    measured = kept.load(now_ms)
    assert kept.queue.prune(now_ms) == 0
    assert set(run.engine.queue.offers(0)) == {"fresh", "stale"}
    assert measured.workers_ready == 1
    assert measured == run.engine.load(now_ms)
    assert set(run.engine.queue.offers(0)) == {"fresh"}


@pytest.mark.feature("lab-scheduler-load-measure")
def test_the_measure_times_each_build_on_both_clocks_and_puts_the_scheduler_parser_back_even_when_a_build_raises(
    lab: Callable[..., Run],
) -> None:
    run = _live(lab)
    parse = scheduler._parse
    engine = load_measure.reader(run.lab.client)
    process = iter([1.0, 1.5, 2.0, 2.25]).__next__
    wall = iter([10.0, 12.0, 20.0, 21.0]).__next__
    figures = load_measure.measure(engine, 2, 1000, process, wall)
    assert (figures["process_seconds"], figures["wall_seconds"]) == ([0.5, 0.25], [2.0, 1.0])
    assert scheduler._parse is parse
    run.lab.client.sadd(scheduler.LIVE, "broken")
    run.lab.client.set(scheduler.GRADES + "broken", "{")
    with pytest.raises(json.JSONDecodeError):
        load_measure.measure(engine, 1, 1000, time.process_time, time.perf_counter)
    assert scheduler._parse is parse
