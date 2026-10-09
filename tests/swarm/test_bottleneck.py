import json

import pytest

from scripts.swarm import bottleneck
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit
SLUG = "scratch"
H = 3_600_000
NOW = 10 * H
HOST_REASON = "holding spawns: host load room 0, 3 spawned since it was granted: load is high"
QUOTA_REASON = "accounts are limited; Claude has 0 free seats and Codex has 0 free seats"


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


def run(branch, created, ended, conclusion, queue_s=600.0):
    return {
        "ledger": SLUG,
        "branch": branch,
        "ts_ms": int(ended),
        "queue_s": queue_s,
        "run_s": (ended - created) / 1000 - queue_s,
        "conclusion": conclusion,
    }


def sample(at, held, reason):
    return {"ledger": SLUG, "ts_ms": at, "held_spawns": held, "reason": reason}


def every_five_minutes(start, end, held, reason):
    return [sample(at, held, reason) for at in range(start, end, bottleneck.SAMPLE_GAP_MS)]


TASK = {"id": "t1", "state": "done", "branch": "b1"}


def ci_heavy():
    return rows(
        agent=[claim("t1", 5 * H)],
        delivery=[merge("t1", 9.75 * H)],
        ci=[run("b1", 6.5 * H, 8 * H, "failure"), run("b1", 8.25 * H, 9.5 * H, "success")],
        host=[sample(7 * H, 0, QUOTA_REASON)],
    )


def test_a_ci_heavy_window_names_ci_with_its_numbers():
    found = bottleneck.report(ci_heavy(), [TASK], NOW)
    assert found == {
        "at": NOW,
        "window_hours": 4.0,
        "seconds": {"engineering": 1800.0, "ci": 10800.0, "review": 900.0, "host": 0.0, "quota": 0.0},
        "total": 13500.0,
        "bottleneck": "ci",
    }
    assert bottleneck.line(found, NOW) == (
        "bottleneck CI, push to green checks: 80 percent of 3.8 hours in the last 4 hours, measured 0 minutes ago;"
        " engineering 13 percent, review and queue 7 percent, host held spawns 0 percent, quota held spawns 0 percent"
    )


def test_a_host_held_window_names_host():
    found = bottleneck.report(
        rows(
            agent=[claim("t2", 9 * H)],
            host=every_five_minutes(6 * H, NOW, 2, HOST_REASON),
        ),
        [{"id": "t2", "state": "claimed", "branch": "b2"}],
        NOW,
    )
    assert found["seconds"] == {"engineering": 3600.0, "ci": 0.0, "review": 0.0, "host": 28800.0, "quota": 0.0}
    assert found["bottleneck"] == "host"


def test_spawns_held_for_any_other_reason_count_as_quota():
    found = bottleneck.report(rows(host=[sample(9 * H, 3, QUOTA_REASON)]), [], NOW)
    assert found["seconds"]["quota"] == 900.0
    assert found["bottleneck"] == "quota"


def test_a_sample_covers_at_most_one_gap_and_stops_at_now():
    found = bottleneck.report(
        rows(host=[sample(7 * H, 1, HOST_REASON), sample(8 * H, 1, HOST_REASON), sample(NOW - 60_000, 1, HOST_REASON)]),
        [],
        NOW,
    )
    assert found["seconds"]["host"] == 660.0


def test_time_before_the_window_is_not_counted():
    found = bottleneck.report(
        rows(agent=[claim("t1", 2 * H)], delivery=[merge("t1", 7 * H)]),
        [TASK],
        NOW,
    )
    assert found["seconds"]["engineering"] == 3600.0


def test_an_open_task_before_its_first_run_finishes_counts_ci_from_its_pull_request():
    opened = {"ledger": SLUG, "task": "t3", "kind": "pull_request_opened", "ts_ms": 8 * H}
    found = bottleneck.report(
        rows(agent=[claim("t3", 7 * H)], delivery=[opened]),
        [{"id": "t3", "state": "pr", "branch": "b3"}],
        NOW,
    )
    assert found["seconds"] == {"engineering": 3600.0, "ci": 7200.0, "review": 0.0, "host": 0.0, "quota": 0.0}


def test_a_green_run_then_a_red_run_keeps_ci_holding_until_the_next_green():
    found = bottleneck.report(
        rows(
            agent=[claim("t4", 6 * H)],
            ci=[
                run("b4", 7 * H, 7.5 * H, "success"),
                run("b4", 7.75 * H, 8 * H, "failure"),
                run("b4", 8.5 * H, 9 * H, "success"),
            ],
        ),
        [{"id": "t4", "state": "pr", "branch": "b4"}],
        NOW,
    )
    assert found["seconds"] == {"engineering": 3600.0, "ci": 7200.0, "review": 3600.0, "host": 0.0, "quota": 0.0}


def test_runs_before_the_claim_and_on_other_branches_are_ignored():
    found = bottleneck.report(
        rows(
            agent=[claim("t5", 8 * H)],
            ci=[run("b5", 6 * H, 7 * H, "success"), run("other", 8.5 * H, 9 * H, "success")],
        ),
        [{"id": "t5", "state": "claimed", "branch": "b5"}],
        NOW,
    )
    assert found["seconds"]["engineering"] == 7200.0
    assert found["bottleneck"] == "engineering"


def test_a_closed_task_without_a_merge_ends_at_its_last_agent_row():
    retire = {"ledger": SLUG, "task": "t6", "kind": "retire", "ts_ms": 8 * H}
    found = bottleneck.report(rows(agent=[claim("t6", 7 * H), retire]), [{"id": "t6", "state": "done"}], NOW)
    assert found["seconds"]["engineering"] == 3600.0


def test_an_empty_window_names_nothing():
    found = bottleneck.report(rows(), [], NOW)
    assert found["bottleneck"] == ""
    assert found["total"] == 0.0
    assert bottleneck.line(found, NOW) == "bottleneck none: no delivery time or held spawns in the last 4 hours"


def test_an_unmeasured_swarm_says_so():
    assert bottleneck.line({}, NOW) == "bottleneck not measured yet: the metrics sink is off or no tick has run"


def test_ties_resolve_in_delivery_order():
    found = bottleneck.report(
        rows(agent=[claim("t7", 8 * H)], host=[sample(NOW - 300_000, 24, HOST_REASON)]),
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
            theirs = [{**row, "ledger": "other", "event_id": row["event_id"] + ":other"} for row in mine]
            box.append(table, mine + theirs)
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
    assert bottleneck.line(found, NOW + 25 * 60_000).startswith(
        "bottleneck CI, push to green checks: 80 percent of 3.8 hours in the last 4 hours, measured 25 minutes ago;"
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
