import json

import pytest

from scripts.swarm import bottleneck
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit
SLUG = "scratch"
H = 3_600_000
NOW = 10 * H


def rows(agent=(), delivery=(), ci=(), host=()):
    return {
        "agent_events": list(agent),
        "delivery_events": list(delivery),
        "ci_runs": list(ci),
        "host_samples": list(host),
    }


def claim(task, at):
    return {"ledger": SLUG, "task": task, "kind": "claim", "ts_ms": int(at)}


def merge(task, at):
    return {"ledger": SLUG, "task": task, "kind": "merge", "ts_ms": int(at)}


def opened(task, at):
    return {"ledger": SLUG, "task": task, "kind": "pull_request_opened", "ts_ms": int(at)}


def run(branch, created, ended, conclusion, queue_s=600.0):
    return {
        "ledger": SLUG,
        "branch": branch,
        "ts_ms": int(ended),
        "queue_s": queue_s,
        "run_s": (ended - created) / 1000 - queue_s,
        "conclusion": conclusion,
    }


def sample(at, held, held_by):
    return {"ledger": SLUG, "ts_ms": int(at), "held_spawns": held, "held_by": held_by}


def every_five_minutes(start, end, held, held_by):
    return [sample(at, held, held_by) for at in range(start, end, bottleneck.SAMPLE_GAP_MS)]


def seconds(engineering=0.0, ci=0.0, review=0.0, host=0.0, quota=0.0):
    return {"engineering": engineering, "ci": ci, "review": review, "host": host, "quota": quota}


TASK = {"id": "t1", "state": "done", "branch": "b1"}


def ci_heavy():
    return rows(
        agent=[claim("t1", 5 * H)],
        delivery=[merge("t1", 9.75 * H)],
        ci=[run("b1", 6.5 * H, 8 * H, "failure"), run("b1", 8.25 * H, 9.5 * H, "success")],
        host=[sample(7 * H, 0, "")],
    )


def open_task(task, state="pr"):
    return [{"id": task, "state": state, "branch": f"b-{task}"}]


def test_a_ci_heavy_window_names_ci_with_its_numbers():
    found = bottleneck.report(ci_heavy(), [TASK], NOW)
    assert found == {
        "at": NOW,
        "window_hours": 4.0,
        "seconds": seconds(1800.0, 10800.0, 900.0),
        "total": 13500.0,
        "bottleneck": "ci",
    }
    assert bottleneck.line(found, NOW) == (
        "bottleneck CI, push to green checks: 80 percent of 3.8 task hours in the last 4 hours, measured 0 minutes"
        " ago; engineering 13 percent, review and queue 7 percent, host held spawns 0 percent, quota held spawns"
        " 0 percent"
    )


def test_a_host_held_window_names_host():
    found = bottleneck.report(
        rows(agent=[claim("t2", 9 * H)], host=every_five_minutes(6 * H, NOW, 2, "host")),
        open_task("t2", "claimed"),
        NOW,
    )
    assert found["seconds"] == seconds(engineering=3600.0, host=28800.0)
    assert found["bottleneck"] == "host"


def test_spawns_held_by_quota_count_as_quota():
    found = bottleneck.report(rows(host=[sample(9 * H, 3, "quota")]), [], NOW)
    assert found["seconds"] == seconds(quota=900.0)
    assert found["bottleneck"] == "quota"


def test_a_sample_spooled_before_the_held_by_column_counts_as_quota():
    older = {"ledger": SLUG, "ts_ms": 9 * H, "held_spawns": 2, "reason": "accounts have quota"}
    assert bottleneck.report(rows(host=[older]), [], NOW)["seconds"] == seconds(quota=600.0)


def test_a_sample_covers_at_most_one_gap_and_stops_at_now_in_any_row_order():
    samples = [sample(NOW - 60_000, 1, "host"), sample(8 * H, 1, "host"), sample(7 * H, 1, "host")]
    assert bottleneck.report(rows(host=samples), [], NOW)["seconds"] == seconds(host=660.0)


def test_time_before_the_window_is_not_counted():
    found = bottleneck.report(rows(agent=[claim("t1", 2 * H)], delivery=[merge("t1", 7 * H)]), [TASK], NOW)
    assert found["seconds"] == seconds(engineering=3600.0)


def test_a_task_wholly_before_the_window_adds_nothing():
    found = bottleneck.report(rows(agent=[claim("t1", 2 * H)], delivery=[merge("t1", 3 * H)]), [TASK], NOW)
    assert (found["total"], found["bottleneck"]) == (0.0, "")


def test_an_open_task_before_its_first_run_finishes_counts_ci_from_its_pull_request():
    found = bottleneck.report(rows(agent=[claim("t3", 7 * H)], delivery=[opened("t3", 8 * H)]), open_task("t3"), NOW)
    assert found["seconds"] == seconds(3600.0, 7200.0)


def test_the_first_claim_and_first_pull_request_since_it_bound_the_spans_in_any_row_order():
    found = bottleneck.report(
        rows(
            agent=[claim("t3", 7 * H), claim("t3", 8 * H)],
            delivery=[opened("t3", 9 * H), opened("t3", 6 * H), opened("t3", 8.5 * H)],
        ),
        open_task("t3"),
        NOW,
    )
    assert found["seconds"] == seconds(5400.0, 5400.0)


def test_a_green_run_then_a_red_run_keeps_ci_holding_until_the_next_green():
    runs = [
        run("b-t4", 8.5 * H, 9 * H, "success"),
        run("b-t4", 7.75 * H, 8 * H, "failure"),
        run("b-t4", 7 * H, 7.5 * H, "success"),
    ]
    found = bottleneck.report(rows(agent=[claim("t4", 6 * H)], ci=runs), open_task("t4"), NOW)
    assert found["seconds"] == seconds(3600.0, 7200.0, 3600.0)


def test_a_single_green_run_moves_the_task_to_review():
    found = bottleneck.report(
        rows(agent=[claim("t4", 6 * H)], ci=[run("b-t4", 7 * H, 8 * H, "success")]), open_task("t4"), NOW
    )
    assert found["seconds"] == seconds(3600.0, 3600.0, 7200.0)


def test_a_cancelled_last_run_keeps_ci_holding_until_now():
    found = bottleneck.report(
        rows(agent=[claim("t4", 6 * H)], ci=[run("b-t4", 7 * H, 8 * H, "cancelled")]), open_task("t4"), NOW
    )
    assert found["seconds"] == seconds(3600.0, 10800.0)


def test_a_run_created_at_the_claim_is_the_first_push():
    found = bottleneck.report(
        rows(agent=[claim("t1", 7 * H)], delivery=[merge("t1", 9 * H)], ci=[run("b1", 7 * H, 8 * H, "success")]),
        [TASK],
        NOW,
    )
    assert found["seconds"] == seconds(0.0, 3600.0, 3600.0)


def test_runs_before_the_claim_and_on_other_branches_are_ignored():
    found = bottleneck.report(
        rows(
            agent=[claim("t5", 8 * H)],
            ci=[run("b-t5", 6 * H, 7 * H, "success"), run("other", 8.5 * H, 9 * H, "success")],
        ),
        open_task("t5", "claimed"),
        NOW,
    )
    assert found["seconds"] == seconds(engineering=7200.0)
    assert found["bottleneck"] == "engineering"


def test_a_closed_task_without_a_merge_ends_at_its_last_agent_row():
    retire = {"ledger": SLUG, "task": "t6", "kind": "retire", "ts_ms": 8 * H}
    found = bottleneck.report(rows(agent=[retire, claim("t6", 7 * H)]), [{"id": "t6", "state": "done"}], NOW)
    assert found["seconds"] == seconds(engineering=3600.0)


def test_a_run_ending_at_the_first_push_is_green_at_once():
    found = bottleneck.report(
        rows(agent=[claim("t4", 7 * H)], delivery=[opened("t4", 8 * H)], ci=[run("b-t4", 6.5 * H, 8 * H, "success")]),
        open_task("t4"),
        NOW,
    )
    assert found["seconds"] == seconds(3600.0, 0.0, 7200.0)


def test_a_red_run_ending_at_the_merge_keeps_ci_holding_to_the_merge():
    runs = [run("b1", 7 * H, 8 * H, "success"), run("b1", 8.5 * H, 9 * H, "failure")]
    found = bottleneck.report(rows(agent=[claim("t1", 6 * H)], delivery=[merge("t1", 9 * H)], ci=runs), [TASK], NOW)
    assert found["seconds"] == seconds(3600.0, 7200.0, 0.0)


def test_the_first_of_two_green_runs_ends_ci():
    runs = [run("b-t4", 7 * H, 7.5 * H, "success"), run("b-t4", 8 * H, 8.5 * H, "success")]
    found = bottleneck.report(rows(agent=[claim("t4", 6 * H)], ci=runs), open_task("t4"), NOW)
    assert found["seconds"] == seconds(3600.0, 1800.0, 9000.0)


def test_every_task_and_every_span_adds_to_its_share():
    found = bottleneck.report(
        rows(agent=[claim("t5", 8 * H), claim("t6", 9 * H)]),
        [*open_task("t5", "claimed"), *open_task("t6", "claimed")],
        NOW,
    )
    assert found["seconds"] == seconds(engineering=10800.0)


def test_shares_and_total_keep_a_tenth_of_a_second():
    found = bottleneck.report(rows(host=[sample(NOW - 1_234, 1, "quota")]), [], NOW)
    assert (found["seconds"], found["total"]) == (seconds(quota=1.2), 1.2)


def test_an_empty_window_names_nothing():
    found = bottleneck.report(rows(), [], NOW)
    assert found["bottleneck"] == ""
    assert found["total"] == 0.0
    assert bottleneck.line(found, NOW) == "bottleneck none: no delivery time or held spawns in the last 4 hours"


def test_an_unmeasured_swarm_says_so():
    assert bottleneck.line({}, NOW) == "bottleneck not measured yet: the metrics sink is off or no tick has run"


def test_ties_resolve_in_delivery_order():
    found = bottleneck.report(
        rows(agent=[claim("t7", 8 * H)], host=[sample(NOW - 300_000, 24, "host")]),
        [{"id": "t7", "state": "claimed"}],
        NOW,
    )
    assert found["seconds"]["engineering"] == found["seconds"]["host"] == 7200.0
    assert found["bottleneck"] == "engineering"


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, ".", 0, 0))
    return found


def test_record_writes_a_bottleneck_row_and_the_feed_from_this_ledgers_rows(tmp_path, store):
    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "u", ""))
    try:
        base = {"event_id": "", "plan": "", "phase": "", "slice": "", "task": ""}
        from scripts.swarm import metrics_ci, metrics_swarm

        def stored(table, row, number):
            schema = dict(table.columns)
            full = {**base, **{k: ("" if v == "String" else 0) for k, v in schema.items()}, **row}
            return {**full, "event_id": f"{table.name}:{number}"}

        found = ci_heavy()
        tables = {
            "agent_events": metrics_swarm.AGENTS,
            "delivery_events": metrics_swarm.DELIVERY,
            "ci_runs": metrics_ci.RUNS,
            "host_samples": metrics_swarm.HOST,
        }
        for name, table in tables.items():
            mine = [stored(table, row, n) for n, row in enumerate(found[name])]
            box.append(table, mine)
        held = stored(metrics_swarm.HOST, sample(9 * H, 50, "host"), 99)
        box.append(metrics_swarm.HOST, [{**held, "ledger": "other"}])
        recorded = bottleneck.record(box, store, SLUG, NOW, [TASK])
        assert recorded == bottleneck.report(ci_heavy(), [TASK], NOW)
        assert bottleneck.read(store, SLUG) == recorded
        assert json.loads(store.redis.get(store.key(SLUG, "bottleneck"))) == recorded
        assert box.recent("bottlenecks", NOW) == [
            {
                "event_id": f"bottleneck:{SLUG}:{NOW}",
                "ledger": SLUG,
                "ts_ms": NOW,
                "plan": "",
                "phase": "",
                "slice": "",
                "task": "",
                "bottleneck": "ci",
                "engineering_s": 1800.0,
                "ci_s": 10800.0,
                "review_s": 900.0,
                "host_s": 0.0,
                "quota_s": 0.0,
                "total_s": 13500.0,
            }
        ]
    finally:
        box.close()


def test_read_is_empty_before_any_record(store):
    assert bottleneck.read(store, SLUG) == {}


def test_the_line_reports_the_age_of_the_measure():
    found = bottleneck.report(ci_heavy(), [TASK], NOW)
    assert bottleneck.line({**found, "seconds": seconds(1800.0, 10800.0, 0.0, 450.0, 450.0)}, NOW + 25 * 60_000) == (
        "bottleneck CI, push to green checks: 80 percent of 3.8 task hours in the last 4 hours, measured 25 minutes"
        " ago; engineering 13 percent, review and queue 0 percent, host held spawns 3 percent, quota held spawns"
        " 3 percent"
    )


def test_swarm_status_prints_the_bottleneck_line(store, monkeypatch, capsys):
    from scripts.swarm import cli
    from tests.swarm.test_tick import FakeLedger

    found = bottleneck.report(ci_heavy(), [TASK], NOW)
    store.redis.set(store.key(SLUG, "bottleneck"), json.dumps(found))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(cli, "now_ms", lambda: NOW + 25 * 60_000)
    assert cli.main([SLUG, "status"]) == 0
    assert bottleneck.line(found, NOW + 25 * 60_000) in capsys.readouterr().out.splitlines()


def test_the_status_report_feeds_the_bottleneck_to_its_readers(store, monkeypatch):
    from scripts.swarm import status

    found = bottleneck.report(ci_heavy(), [TASK], NOW)
    store.redis.set(store.key(SLUG, "bottleneck"), json.dumps(found))
    monkeypatch.setattr(status, "page_quota", lambda: {})
    assert status.status_report(store, SLUG, {"tasks": []})["bottleneck"] == found
