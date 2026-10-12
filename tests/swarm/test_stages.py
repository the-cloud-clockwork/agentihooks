import json

import pytest

from scripts.swarm import stages
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
SLUG = "scratch"
M = 60_000
H = 60 * M
NOW = 10 * H


def at(hours, minutes=0, seconds=0):
    return int(hours * H + minutes * M + seconds * 1000)


def agent(kind, task, ts, name="", harness=""):
    return {"ledger": SLUG, "task": task, "kind": kind, "ts_ms": ts, "agent": name, "harness": harness}


def delivery(kind, task, ts):
    return {"ledger": SLUG, "task": task, "kind": kind, "ts_ms": ts}


def ledger(kind, ts, target="", by="swarm"):
    return {"ledger": SLUG, "kind": kind, "ts_ms": ts, "target": target, "by": by}


def run(run_id, branch, created, ended, conclusion, queue_s=0.0):
    return {
        "ledger": SLUG,
        "run_id": run_id,
        "attempt": 1,
        "branch": branch,
        "ts_ms": ended,
        "queue_s": queue_s,
        "run_s": (ended - created) / 1000 - queue_s,
        "conclusion": conclusion,
    }


def stage(run_id, name, seconds, budget, pickup_s, runner):
    return {
        "ledger": SLUG,
        "ts_ms": at(8, 30),
        "run_id": run_id,
        "attempt": 1,
        "stage": name,
        "seconds": seconds,
        "budget": budget,
        "pickup_s": pickup_s,
        "runner": runner,
    }


def sample(ts, held, held_by):
    return {"ledger": SLUG, "ts_ms": ts, "held_spawns": held, "held_by": held_by}


def rows(**found):
    return {name: list(found.get(name, ())) for name in stages.SOURCES}


TASK = {"id": "t1", "state": "done", "branch": "b1", "depends_on": ["t0"]}


def every_stage():
    return rows(
        ledger_events=[
            ledger("added", at(1), "tasks/t1"),
            ledger("task done", at(6), "tasks/t0"),
            ledger("joined", at(7), by="e2"),
        ],
        host_samples=[
            sample(at(6), 1, "quota"),
            sample(at(6, 5), 1, "quota"),
            sample(at(6, 10), 1, "quota"),
            sample(at(6, 15), 0, ""),
        ],
        agent_events=[
            agent("claim", "t1", at(6, 30)),
            agent("spawn", "t1", at(6, 30), "e1", "claude"),
            agent("launch_failed", "t1", at(6, 30), "e1", "claude"),
            agent("retire", "t1", at(6, 40), "e1", "claude"),
            agent("claim", "t1", at(6, 50)),
            agent("spawn", "t1", at(6, 50), "e2", "codex"),
        ],
        ci_runs=[
            run(1, "b1", at(8), at(8, 30), "failure", queue_s=300.0),
            run(2, "b1", at(8, 40), at(9), "success"),
        ],
        ci_stages=[
            stage(1, "unit", 400.0, 300, 120.0, "anton-k3s"),
            stage(1, "lint", 100.0, 240, 60.0, "ubuntu-latest"),
        ],
        delivery_events=[
            delivery("pull_request_opened", "t1", at(8, 1)),
            delivery("intent_failed", "t1", at(9, 5)),
            delivery("intent_passed", "t1", at(9, 15)),
            delivery("merge_queued", "t1", at(9, 30)),
            delivery("merge", "t1", at(9, 45)),
        ],
    )


def seconds_of(found):
    return {(row["stage"], row["label"]): row["seconds"] for row in found["stages"]}


def test_every_task_second_of_a_recorded_task_lands_in_exactly_one_named_stage():
    found = stages.report(every_stage(), [TASK], NOW)
    assert seconds_of(found) == {
        ("claim_wait", ""): 1500.0,
        ("quota_hold", ""): 900.0,
        ("launch_failure", "claude"): 600.0,
        ("launch", "codex"): 600.0,
        ("build", ""): 3600.0,
        ("ci_queue", "anton-k3s"): 420.0,
        ("ci_overrun", "unit"): 100.0,
        ("ci_run", ""): 3080.0,
        ("intent_fix", ""): 600.0,
        ("merge_queue", ""): 900.0,
        ("review", "round 1"): 1200.0,
    }
    assert found["total"] == 13500.0 == sum(seconds_of(found).values())
    assert [row["seconds"] for row in found["stages"]] == sorted(
        (row["seconds"] for row in found["stages"]), reverse=True
    )


def test_stages_are_ordered_by_seconds_then_name_and_label():
    found = stages.report(every_stage(), [TASK], NOW)
    assert [(row["stage"], row["label"]) for row in found["stages"][:3]] == [
        ("build", ""),
        ("ci_run", ""),
        ("claim_wait", ""),
    ]


def test_the_total_is_the_sum_of_task_spans_inside_the_window_for_many_tasks():
    base = every_stage()
    shifted = rows(
        agent_events=[agent("claim", "t2", at(4)), agent("spawn", "t2", at(4), "e3", "claude")],
        ledger_events=[ledger("joined", at(4, 10), by="e3")],
    )
    merged = {name: base[name] + shifted[name] for name in stages.SOURCES}
    tasks = [TASK, {"id": "t2", "state": "claimed", "branch": "b2"}]
    found = stages.report(merged, tasks, NOW)
    assert found["total"] == 13500.0 + 4 * 3600.0
    assert seconds_of(found)[("build", "")] == 3600.0 + 4 * 3600.0
    assert sum(seconds_of(found).values()) == found["total"]


def test_host_held_claim_wait_is_host_hold():
    found = stages.report(
        rows(
            ledger_events=[ledger("task done", at(7), "tasks/t0"), ledger("joined", at(8), by="e1")],
            host_samples=[sample(at(7), 2, "host"), sample(at(7, 5), 0, "")],
            agent_events=[agent("claim", "t1", at(8)), agent("spawn", "t1", at(8), "e1", "claude")],
        ),
        [{**TASK, "state": "claimed"}],
        NOW,
    )
    assert seconds_of(found) == {
        ("host_hold", ""): 300.0,
        ("claim_wait", ""): 3300.0,
        ("build", ""): 7200.0,
    }


def test_an_open_ready_task_waits_for_its_claim_until_now():
    tasks = [{"id": "t0", "state": "done"}, {"id": "t3", "state": "open", "depends_on": ["t0"]}]
    found = stages.report(rows(ledger_events=[ledger("task done", at(8), "tasks/t0")]), tasks, NOW)
    assert seconds_of(found) == {("claim_wait", ""): 7200.0}


def test_an_open_task_with_an_unfinished_dependency_is_not_waiting():
    tasks = [{"id": "t0", "state": "claimed"}, {"id": "t3", "state": "open", "depends_on": ["t0"]}]
    found = stages.report(rows(ledger_events=[ledger("added", at(8), "tasks/t3")]), tasks, NOW)
    assert found["stages"] == [] and found["total"] == 0.0


def test_a_task_without_ready_evidence_starts_at_its_claim():
    found = stages.report(
        rows(agent_events=[agent("claim", "t1", at(9)), agent("spawn", "t1", at(9), "e1", "claude")]),
        [{**TASK, "state": "claimed"}],
        NOW,
    )
    assert seconds_of(found) == {("launch", "claude"): 3600.0}


def test_a_reopen_after_the_claim_does_not_move_the_ready_time():
    found = stages.report(
        rows(
            ledger_events=[ledger("added", at(8), "tasks/t1"), ledger("task open", at(9), "tasks/t1")],
            agent_events=[agent("claim", "t1", at(8, 30))],
        ),
        [{**TASK, "state": "claimed"}],
        NOW,
    )
    assert seconds_of(found)[("claim_wait", "")] == 1800.0 + 5400.0


def test_time_before_the_window_is_not_counted():
    found = stages.report(every_stage(), [TASK], at(13))
    assert found["total"] == (at(9, 45) - at(9)) / 1000
    assert seconds_of(found) == {
        ("intent_fix", ""): 600.0,
        ("merge_queue", ""): 900.0,
        ("review", "round 1"): 1200.0,
    }


def test_a_run_with_no_stage_rows_queues_for_its_own_queue_time():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(7)), agent("spawn", "t1", at(7), "e1", "claude")],
            ledger_events=[ledger("joined", at(7), by="e1")],
            ci_runs=[run(1, "b1", at(8), at(8, 20), "success", queue_s=120.0)],
            delivery_events=[delivery("merge", "t1", at(8, 30))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {
        ("build", ""): 3600.0,
        ("ci_queue", ""): 120.0,
        ("ci_run", ""): 1080.0,
        ("review", "round 1"): 600.0,
    }


def test_an_overrun_larger_than_the_run_is_capped_at_the_run():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(7)), agent("spawn", "t1", at(7), "e1", "claude")],
            ledger_events=[ledger("joined", at(7), by="e1")],
            ci_runs=[run(1, "b1", at(8), at(8, 10), "success")],
            ci_stages=[stage(1, "mutation", 5000.0, 480, 0.0, "anton-k3s")],
            delivery_events=[delivery("merge", "t1", at(8, 10))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {("build", ""): 3600.0, ("ci_overrun", "mutation"): 600.0}


def test_an_intent_failure_without_a_later_verdict_holds_until_the_merge():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(9)), agent("spawn", "t1", at(9), "e1", "claude")],
            ledger_events=[ledger("joined", at(9), by="e1")],
            ci_runs=[run(1, "b1", at(9, 10), at(9, 20), "success")],
            delivery_events=[delivery("intent_failed", "t1", at(9, 30)), delivery("merge", "t1", at(9, 50))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found)[("intent_fix", "")] == 1200.0


def test_a_task_merged_before_the_window_adds_nothing():
    found = stages.report(
        rows(agent_events=[agent("claim", "t1", at(2))], delivery_events=[delivery("merge", "t1", at(5))]),
        [TASK],
        NOW,
    )
    assert found["stages"] == [] and found["total"] == 0.0


def test_events_of_other_items_do_not_move_the_ready_time():
    found = stages.report(
        rows(
            ledger_events=[
                ledger("task done", at(7), "tasks/t0"),
                ledger("added", at(7, 30), "followups/f1"),
                ledger("task done", at(7, 40), "tasks/t9"),
            ],
            agent_events=[agent("claim", "t1", at(8))],
        ),
        [{**TASK, "state": "claimed"}],
        at(8),
    )
    assert seconds_of(found) == {("claim_wait", ""): 3600.0}


def test_launch_ends_at_the_agents_own_join_not_another_agents():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(9)), agent("spawn", "t1", at(9), "e1", "claude")],
            ledger_events=[ledger("joined", at(9, 10), by="e9"), ledger("joined", at(9, 30), by="e1")],
        ),
        [{**TASK, "state": "claimed"}],
        NOW,
    )
    assert seconds_of(found) == {("launch", "claude"): 1800.0, ("build", ""): 1800.0}


def test_a_closed_task_without_a_merge_ends_at_its_last_agent_row():
    found = stages.report(
        rows(
            agent_events=[
                agent("claim", "t1", at(8)),
                agent("spawn", "t1", at(8), "e1", "claude"),
                agent("retire", "t1", at(9), "e1", "claude"),
            ],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {("launch", "claude"): 3600.0}


def test_a_pull_request_opened_before_its_first_run_is_the_first_push():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(8)), agent("spawn", "t1", at(8), "e1", "claude")],
            ledger_events=[ledger("joined", at(8), by="e1")],
            ci_runs=[run(1, "b1", at(8, 45), at(9), "success")],
            delivery_events=[delivery("pull_request_opened", "t1", at(8, 30)), delivery("merge", "t1", at(9, 30))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {("build", ""): 1800.0, ("ci_run", ""): 1800.0, ("review", "round 1"): 1800.0}


def test_a_run_created_at_the_claim_is_the_first_push():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(8)), agent("spawn", "t1", at(8), "e1", "claude")],
            ledger_events=[ledger("joined", at(8), by="e1")],
            ci_runs=[run(1, "b1", at(8), at(8, 20), "success")],
            delivery_events=[delivery("merge", "t1", at(8, 30))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {("ci_run", ""): 1200.0, ("review", "round 1"): 600.0}


def test_a_claimed_task_missing_from_the_ledger_is_still_measured():
    found = stages.report(
        rows(
            agent_events=[
                agent("claim", "tx", at(9)),
                agent("spawn", "tx", at(9), "e1", "claude"),
                agent("retire", "tx", at(9, 30), "e1", "claude"),
            ],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {("launch", "claude"): 1800.0}


def test_adjacent_host_and_quota_holds_stay_apart_and_empty_samples_hold_nothing():
    found = stages.report(
        rows(
            ledger_events=[ledger("task done", at(7), "tasks/t0")],
            host_samples=[sample(at(7), 1, "host"), sample(at(7, 5), 1, "quota"), sample(at(7, 10), 0, "")],
            agent_events=[agent("claim", "t1", at(8))],
        ),
        [{**TASK, "state": "claimed"}],
        at(8),
    )
    assert seconds_of(found) == {("host_hold", ""): 300.0, ("quota_hold", ""): 300.0, ("claim_wait", ""): 3000.0}


def test_holds_separated_by_a_sample_gap_do_not_cover_the_gap():
    found = stages.report(
        rows(
            ledger_events=[ledger("task done", at(7), "tasks/t0")],
            host_samples=[
                sample(at(7), 1, "quota"),
                sample(at(7, 20), 1, "quota"),
                sample(at(7, 25), 0, ""),
            ],
            agent_events=[agent("claim", "t1", at(8))],
        ),
        [{**TASK, "state": "claimed"}],
        at(8),
    )
    assert seconds_of(found) == {("quota_hold", ""): 600.0, ("claim_wait", ""): 3000.0}


def test_quota_holds_carry_the_held_harness_and_split_where_it_changes():
    found = stages.report(
        rows(
            ledger_events=[ledger("task done", at(7), "tasks/t0")],
            host_samples=[
                {**sample(at(7), 1, "quota"), "held_harness": "codex"},
                {**sample(at(7, 5), 1, "quota"), "held_harness": "codex"},
                {**sample(at(7, 10), 1, "quota"), "held_harness": "claude"},
                sample(at(7, 15), 1, "host"),
                sample(at(7, 20), 0, ""),
            ],
            agent_events=[agent("claim", "t1", at(8))],
        ),
        [{**TASK, "state": "claimed"}],
        at(8),
    )
    assert seconds_of(found) == {
        ("quota_hold", "codex"): 600.0,
        ("quota_hold", "claude"): 300.0,
        ("host_hold", ""): 300.0,
        ("claim_wait", ""): 2400.0,
    }


def test_a_reopen_at_the_claim_instant_is_the_ready_time():
    found = stages.report(
        rows(
            ledger_events=[ledger("added", at(7), "tasks/t1"), ledger("task open", at(8), "tasks/t1")],
            agent_events=[agent("claim", "t1", at(8))],
        ),
        [{**TASK, "state": "claimed"}],
        at(8),
    )
    assert found["stages"] == [] and found["total"] == 0.0


def test_overruns_that_outgrow_their_run_are_cut_at_the_run_start():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(7)), agent("spawn", "t1", at(7), "e1", "claude")],
            ledger_events=[ledger("joined", at(7), by="e1")],
            ci_runs=[run(1, "b1", at(8), at(8, 10), "success")],
            ci_stages=[
                stage(1, "unit", 980.0, 480, 0.0, "anton-k3s"),
                stage(1, "lint", 540.0, 240, 0.0, "anton-k3s"),
            ],
            delivery_events=[delivery("merge", "t1", at(8, 10))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {("build", ""): 3600.0, ("ci_overrun", "unit"): 500.0, ("ci_overrun", "lint"): 100.0}


def test_a_life_whose_spawn_waited_for_room_is_claim_wait_not_launch():
    found = stages.report(
        rows(
            agent_events=[
                agent("claim", "t1", at(9)),
                agent("spawn", "t1", at(9), "e1", "codex"),
                agent("launch_waiting", "t1", at(9), "e1", "codex"),
                agent("retire", "t1", at(9, 30), "e1", "codex"),
            ],
        ),
        [{**TASK, "state": "claimed"}],
        NOW,
    )
    assert seconds_of(found) == {("claim_wait", ""): 3600.0}


def test_the_merge_queue_starts_at_the_first_tick_that_saw_the_pull_request_queued():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(6)), agent("spawn", "t1", at(6), "e1", "claude")],
            ledger_events=[ledger("joined", at(6), by="e1")],
            ci_runs=[run(1, "b1", at(6, 30), at(7), "success")],
            delivery_events=[
                delivery("merge_queued", "t1", at(7)),
                delivery("merge_queued", "t1", at(8)),
                delivery("merge_queued", "t1", at(9)),
                delivery("merge", "t1", at(9, 30)),
            ],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found)[("merge_queue", "")] == 9000.0


def test_ci_after_the_agent_left_stays_ci_and_every_over_budget_job_gets_its_overrun():
    found = stages.report(
        rows(
            agent_events=[
                agent("claim", "t1", at(6)),
                agent("spawn", "t1", at(6), "e1", "claude"),
                agent("retire", "t1", at(7), "e1", "claude"),
            ],
            ledger_events=[ledger("joined", at(6), by="e1")],
            ci_runs=[run(1, "b1", at(7, 30), at(8, 30), "success", queue_s=600.0)],
            ci_stages=[
                stage(1, "unit", 1080.0, 300, 0.0, "anton-k3s"),
                stage(1, "lint", 290.0, 240, 0.0, "anton-k3s"),
                stage(1, "sonar", 100.0, 240, 0.0, "anton-k3s"),
                stage(1, "fresh", 50.0, 0, 0.0, "anton-k3s"),
            ],
            delivery_events=[delivery("merge", "t1", at(9))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found) == {
        ("build", ""): 3600.0,
        ("claim_wait", ""): 1800.0,
        ("ci_queue", "anton-k3s"): 600.0,
        ("ci_overrun", "unit"): 780.0,
        ("ci_overrun", "lint"): 50.0,
        ("ci_run", ""): 2170.0,
        ("review", "round 1"): 1800.0,
    }
    assert found["total"] == 3 * 3600.0


def test_each_submitted_review_starts_the_next_review_round():
    found = stages.report(
        rows(
            agent_events=[agent("claim", "t1", at(8)), agent("spawn", "t1", at(8), "e1", "claude")],
            ledger_events=[ledger("joined", at(8), by="e1")],
            ci_runs=[run(1, "b1", at(8, 30), at(9), "success")],
            delivery_events=[delivery("review_round", "t1", at(9, 20)), delivery("merge", "t1", at(9, 45))],
        ),
        [TASK],
        NOW,
    )
    assert seconds_of(found)[("review", "round 1")] == 1200.0
    assert seconds_of(found)[("review", "round 2")] == 1500.0
    assert stages.line(found, NOW) == (
        "top stages in the last 4 hours: build 0.5 task hours, ci run 0.5 task hours, review in round 2 0.4 task"
        " hours; 1.8 task hours in all, measured 0 minutes ago"
    )


def test_record_on_an_empty_outbox_measures_nothing(tmp_path, store):
    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "u", ""))
    try:
        found = stages.record(box, store, SLUG, NOW, [TASK])
        assert found["total"] == 0.0
    finally:
        box.close()


def stored(table, row, number):
    base = {"event_id": f"{table.name}:{number}", "plan": "", "phase": "", "slice": "", "task": ""}
    blank = {name: ("" if kind == "String" else 0) for name, kind in table.columns}
    return {**base, **blank, **row}


def test_record_reads_the_outbox_writes_one_row_per_stage_and_keeps_the_feed(tmp_path, store):
    from scripts.swarm import metrics_ci, metrics_ledger, metrics_swarm

    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "u", ""))
    try:
        tables = {
            "agent_events": metrics_swarm.AGENTS,
            "delivery_events": metrics_swarm.DELIVERY,
            "ci_runs": metrics_ci.RUNS,
            "ci_stages": metrics_ci.STAGES,
            "host_samples": metrics_swarm.HOST,
            "ledger_events": metrics_ledger.EVENTS,
        }
        for name, table in tables.items():
            mine = every_stage()[name]
            box.append(table, [stored(table, row, n) for n, row in enumerate(mine)])
        other = stored(metrics_swarm.AGENTS, {**agent("claim", "t1", at(9)), "ledger": "other"}, 99)
        box.append(metrics_swarm.AGENTS, [other])
        recorded = stages.record(box, store, SLUG, NOW, [TASK])
        assert recorded == stages.report(every_stage(), [TASK], NOW)
        assert stages.read(store, SLUG) == recorded
        assert json.loads(store.redis.get(store.key(SLUG, stages.KEY))) == recorded
        exported = box.recent("bottleneck_stages", NOW)
        assert {(row["stage"], row["label"]): row["seconds"] for row in exported} == seconds_of(recorded)
        assert {row["window_s"] for row in exported} == {4 * 3600.0}
        assert all(row["ts_ms"] == NOW and row["ledger"] == SLUG for row in exported)
        assert {(row["plan"], row["phase"], row["slice"], row["task"]) for row in exported} == {("", "", "", "")}
        assert len({row["event_id"] for row in exported}) == len(exported)
    finally:
        box.close()


def test_read_is_empty_before_any_record(store):
    assert stages.read(store, SLUG) == {}


def test_the_line_names_the_top_three_stages_with_task_hours():
    found = stages.report(every_stage(), [TASK], NOW)
    assert stages.line(found, NOW + 5 * M) == (
        "top stages in the last 4 hours: build 1.0 task hours, ci run 0.9 task hours, claim wait 0.4 task hours;"
        " 3.8 task hours in all, measured 5 minutes ago"
    )


def test_the_line_names_labels_of_labelled_stages():
    found = {
        "at": NOW,
        "window_hours": 4.0,
        "total": 7200.0,
        "stages": [
            {"stage": "ci_queue", "label": "anton-k3s", "seconds": 3600.0},
            {"stage": "launch_failure", "label": "codex", "seconds": 1800.0},
            {"stage": "ci_overrun", "label": "mutation-stats", "seconds": 180.0},
        ],
    }
    assert stages.line(found, NOW) == (
        "top stages in the last 4 hours: ci queue on anton-k3s 1.0 task hours, launch failure on codex 0.5 task"
        " hours, ci overrun in mutation-stats 0.1 task hours; 2.0 task hours in all, measured 0 minutes ago"
    )


def test_the_line_says_when_nothing_was_measured():
    assert stages.line({}, NOW) == "top stages not measured yet: the metrics sink is off or no tick has run"
    empty = {"at": NOW, "window_hours": 4.0, "total": 0.0, "stages": []}
    assert stages.line(empty, NOW) == "top stages none: no task time in the last 4 hours"


def test_swarm_status_lists_the_top_stages(monkeypatch, capsys, store):
    from types import SimpleNamespace

    from scripts.swarm import cli
    from tests.swarm.test_tick import FakeLedger

    found = stages.report(every_stage(), [TASK], NOW)
    store.redis.set(store.key(SLUG, stages.KEY), json.dumps(found))
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(cli, "now_ms", lambda: NOW + 5 * M)
    cli.cmd_status(store, SimpleNamespace(slug=SLUG, json=False))
    assert stages.line(found, NOW + 5 * M) in capsys.readouterr().out.splitlines()


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, ".", 0, 0))
    return found
