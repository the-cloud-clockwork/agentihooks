import json
from typing import Any

import pytest

from scripts.swarm import cli, tick_report

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-cost-report")
def test_report_aggregates_step_costs_and_pass_gaps() -> None:
    passes = [
        {"pass_id": "a", "start_ms": 1000, "end_ms": 11000, "wall_s": 10.0},
        {"pass_id": "b", "start_ms": 16000, "end_ms": 36000, "wall_s": 20.0},
    ]
    steps = [
        step("a", "launch", 2.0, 1.0, 1.0),
        step("b", "launch", 6.0, 3.0, 3.0),
        step("b", "inspect", 4.0, 2.0, 0.0),
        step("a", "run_tick", 10.0, 5.0, 5.0),
        step("orphan", "inspect", 2.0, 1.0, 0.0),
    ]
    report = tick_report.summarize(passes, steps)
    assert report == {
        "pass_count": 2,
        "pass_p95_s": 20.0,
        "gap_p95_s": 5.0,
        "gap_max_s": 5.0,
        "passes_without_steps": 0,
        "orphan_steps": 1,
        "steps": [
            {"step": "inspect", "count": 1, "median_s": 4.0, "p95_s": 4.0, "cpu_share": 20.0},
            {"step": "launch", "count": 2, "median_s": 4.0, "p95_s": 6.0, "cpu_share": 80.0},
        ],
    }


def step(pass_id: str, name: str, wall: float, own: float, child: float) -> dict[str, Any]:
    return {"pass_id": pass_id, "step": name, "wall_s": wall, "own_cpu_s": own, "reaped_child_cpu_s": child}


def test_report_reads_only_the_requested_latest_passes() -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for number in range(3):
        record = {"pass_id": str(number), "start_ms": number * 10000, "end_ms": number * 10000 + 1000, "wall_s": 1.0}
        store.redis.xadd(store.key("sw", "tick:passes"), {"record": json.dumps(record)})
        store.redis.xadd(store.key("sw", "tick:steps"), {"record": json.dumps(step(str(number), "launch", 1, 0, 0))})
    report = tick_report.read(store, "sw", 2)
    assert report["pass_count"] == 2
    assert report["steps"] == [{"step": "launch", "count": 2, "median_s": 1.0, "p95_s": 1.0, "cpu_share": 0.0}]
    assert report["orphan_steps"] == 0


def test_empty_report_and_missing_step_history_are_explicit() -> None:
    assert tick_report.summarize([], []) == {
        "pass_count": 0,
        "pass_p95_s": None,
        "gap_p95_s": None,
        "gap_max_s": None,
        "passes_without_steps": 0,
        "orphan_steps": 0,
        "steps": [],
    }
    passes = [{"pass_id": "a", "start_ms": 1000, "end_ms": 2000, "wall_s": 1.0}]
    assert tick_report.summarize(passes, [])["passes_without_steps"] == 1


def test_report_uses_nearest_rank_p95() -> None:
    passes = [{"pass_id": str(n), "start_ms": n * 1000, "end_ms": n * 1000, "wall_s": float(n)} for n in range(1, 21)]
    assert tick_report.summarize(passes, [])["pass_p95_s"] == 19.0


def test_tick_report_parser_preserves_pass_limit() -> None:
    args = cli.build_parser().parse_args(["sw", "tick-report", "--passes", "3"])
    assert args.slug == "sw"
    assert args.passes == 3
    assert args.handler is tick_report.command
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "tick-report", "--passes", "0"])
