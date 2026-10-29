import json

import pytest

from scripts.swarm import stages
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit
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
        ("seat_wait", ""): 1500.0,
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
        ("seat_wait", ""),
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
        ("seat_wait", ""): 3300.0,
        ("build", ""): 7200.0,
    }


def test_an_open_ready_task_waits_for_its_claim_until_now():
    tasks = [{"id": "t0", "state": "done"}, {"id": "t3", "state": "open", "depends_on": ["t0"]}]
    found = stages.report(rows(ledger_events=[ledger("task done", at(8), "tasks/t0")]), tasks, NOW)
    assert seconds_of(found) == {("seat_wait", ""): 7200.0}


def test_an_open_task_with_an_unfinished_dependency_is_not_waiting():
    tasks = [{"id": "t0", "state": "claimed"}, {"id": "t3", "state": "open", "depends_on": ["t0"]}]
    found = stages.report(rows(ledger_events=[ledger("added", at(8), "tasks/t3")]), tasks, NOW)
    assert found["stages"] == [] and found["total"] == 0.0


def waiting_of(tasks, **doc):
    found = stages.report(
        rows(ledger_events=[ledger("added", at(8), f"tasks/{task['id']}") for task in tasks]),
        tasks,
        NOW,
        ready=stages.claimable({"tasks": tasks, **doc}, ""),
    )
    return seconds_of(found)


def test_tick_readiness_keeps_own_focus_nonexclusive_and_needs_no_task_list():
    from scripts.doctor import loop

    task = {"id": "t3", "state": "open", "phase": loop.FIX_PHASE}
    doc = {"tasks": [task], "freezes": [{"verb": "focus", "target": "tasks/t9"}]}
    assert [stages.claimable(doc, loop.FIX_PHASE)(task), stages.claimable(doc, "")(task)] == [True, True]
    assert stages.claimable({}, "")({"id": "t3", "state": "open"}) is True


def test_a_frozen_task_opens_no_claim_wait():
    tasks = [{"id": "t3", "state": "open"}]
    assert waiting_of(tasks, freezes=[{"verb": "freeze", "target": "tasks/t3"}]) == {}


def test_a_task_under_a_frozen_plan_opens_no_claim_wait():
    tasks = [{"id": "t3", "state": "open", "phase": "p1"}]
    doc = {"plans": [{"id": "pl1"}], "phases": [{"id": "p1", "plan": "plans/pl1"}]}
    assert waiting_of(tasks, **doc, freezes=[{"verb": "freeze", "target": "plans/pl1"}]) == {}


def test_a_task_in_a_phase_waiting_on_another_phase_opens_no_claim_wait():
    tasks = [{"id": "t3", "state": "open", "phase": "p2"}]
    assert waiting_of(tasks, phases=[{"id": "p1"}, {"id": "p2", "depends_on": ["p1"]}]) == {}


def test_an_out_of_scope_task_opens_no_claim_wait():
    assert waiting_of([{"id": "t3", "state": "open", "out_of_scope": True}]) == {}


def test_a_parked_task_opens_no_claim_wait():
    tasks = [{"id": "t0", "state": "claimed", "branch": "b0"}, {"id": "t3", "state": "open", "parked_on": ["t0"]}]
    assert waiting_of(tasks) == {}


def test_a_task_the_tick_would_claim_waits_from_its_ready_time():
    tasks = [{"id": "t3", "state": "open", "phase": "p1"}, {"id": "t4", "state": "open", "phase": "p2"}]
    found = waiting_of(
        tasks,
        phases=[{"id": "p1"}, {"id": "p2", "depends_on": ["p1"]}],
        freezes=[{"verb": "freeze", "target": "tasks/t4"}],
    )
    assert found == {("seat_wait", ""): 7200.0}


def test_a_task_stacked_on_a_claimed_branch_waits_like_the_tick_would_claim_it():
    tasks = [{"id": "t0", "state": "claimed", "branch": "b0"}, {"id": "t3", "state": "open", "depends_on": ["t0"]}]
    assert waiting_of(tasks) == {("seat_wait", ""): 7200.0}


def test_without_a_ledger_doc_the_tick_readiness_still_skips_out_of_scope_and_parked_tasks():
    tasks = [
        {"id": "t0", "state": "claimed"},
        {"id": "t1", "state": "open", "out_of_scope": True},
        {"id": "t2", "state": "open", "parked_on": ["t0"]},
        {"id": "t3", "state": "open"},
    ]
    found = stages.report(rows(ledger_events=[ledger("added", at(8), f"tasks/{t['id']}") for t in tasks]), tasks, NOW)
    assert seconds_of(found) == {("seat_wait", ""): 7200.0}
    assert stages.observations(rows(ledger_events=[ledger("added", at(8), "tasks/t1")]), tasks, NOW, 4 * H) == {}


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
    assert seconds_of(found)[("seat_wait", "")] == 1800.0 + 5400.0


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
    assert seconds_of(found) == {("seat_wait", ""): 3600.0}


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
    assert seconds_of(found) == {("host_hold", ""): 300.0, ("quota_hold", ""): 300.0, ("seat_wait", ""): 3000.0}


def test_holds_separated_by_a_sample_gap_do_not_cover_the_gap(monkeypatch):
    monkeypatch.setattr(stages.bottleneck, "SAMPLE_GAP_MS", 5 * 60_000)
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
    assert seconds_of(found) == {("quota_hold", ""): 600.0, ("seat_wait", ""): 3000.0}


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
        ("seat_wait", ""): 2400.0,
    }


def test_a_hold_charges_only_as_many_waiting_tasks_as_spawns_it_held_earliest_ready_first():
    tasks = [
        {"id": "t0", "state": "done"},
        *({"id": f"t{n}", "state": "open", "depends_on": ["t0"]} for n in (2, 3, 4)),
    ]
    held = [
        {**sample(at(9), 1, "quota"), "held_harness": "codex"},
        {**sample(at(9, 5), 2, "quota"), "held_harness": "codex"},
        sample(at(9, 10), 0, ""),
    ]
    source = rows(ledger_events=[ledger("task done", at(8), "tasks/t0")], host_samples=held)
    assert seconds_of(stages.report(source, tasks, NOW)) == {
        ("quota_hold", "codex"): 900.0,
        ("seat_wait", ""): 20700.0,
    }
    charged = {timeline.task: seconds for timeline, seconds in stages._charged(source, tasks, NOW, 4 * H, None)}
    assert [charged[task].get(("quota_hold", "codex"), 0.0) for task in ("t2", "t3", "t4")] == [600.0, 300.0, 0.0]


def test_a_hold_splits_where_a_waiting_task_starts_or_stops_inside_it():
    hold = stages.Span("quota_hold", 100_000, 200_000, "codex", 1)
    starts = (("a", "t1"), 120_000, 300_000, 0, "")
    stops = (("b", "t2"), 50_000, 150_000, 1, "")
    spans = (("c", "t3"), 0, 400_000, 2, "")
    charged, _ = stages._held_pieces([("eng", starts), ("eng", stops), ("eng", spans)], [hold])
    assert charged == [
        (1, "quota_hold", "codex", 20.0),
        (0, "quota_hold", "codex", 30.0),
        (0, "quota_hold", "codex", 50.0),
    ]


def test_a_piece_held_twice_with_a_gap_keeps_the_claim_wait_around_and_between_the_holds():
    piece = (("k", "t1"), 0, 100, 3, "")
    assert stages._uncut(piece, [(50, 60), (10, 20), (15, 30)]) == [
        (("k", "t1"), 0, 10, 3, ""),
        (("k", "t1"), 30, 50, 3, ""),
        (("k", "t1"), 60, 100, 3, ""),
    ]
    assert stages._uncut(piece, [(0, 100)]) == []


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
    assert seconds_of(found) == {("seat_wait", ""): 3600.0}


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
        ("seat_wait", ""): 1800.0,
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
        "top stages in the last 4 hours: build 1.0 task hours, ci run 0.9 task hours, seat wait 0.4 task hours;"
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


def opened(kind, ts, target, **extra):
    return {**ledger(kind, ts, target), "payload": json.dumps({"kind": kind, "target": target, **extra})}


def waited(task, *events, **doc):
    found = stages.report(
        rows(ledger_events=[ledger("added", at(7), f"tasks/{task['id']}"), *events]),
        [task],
        NOW,
        ready=stages.claimable({"tasks": [task], **doc}, ""),
    )
    return seconds_of(found)


OPEN_TASK = {"id": "t3", "state": "open", "phase": "p1"}
PHASES = [{"id": "p1"}]


@pytest.mark.parametrize(
    "events,doc,seconds",
    [
        ([opened("unfrozen", at(9), "tasks/t3", cleared=["tasks/t3"])], {}, 3600.0),
        ([opened("unfrozen", at(9), "phases/p1", cleared=["phases/p1", "tasks/t3"])], {}, 3600.0),
        ([opened("unfrozen", at(9), "tasks/t8", cleared=["tasks/t8"])], {}, 10800.0),
        ([opened("unfrozen", at(9), "phases/p1")], {}, 3600.0),
        ([ledger("unfrozen", at(9), "tasks/t3")], {}, 3600.0),
        (
            [ledger("focused", at(8), "phases/p1")],
            {"freezes": [{"verb": "focus", "target": "phases/p1"}]},
            7200.0,
        ),
        (
            [ledger("focused", at(8), "tasks/t9"), opened("unfrozen", at(9), "tasks/t9", cleared=["tasks/t9"])],
            {},
            3600.0,
        ),
        (
            [
                ledger("focused", at(8), "tasks/t9"),
                opened("unfrozen", at(9), "phases/p9", cleared=["phases/p9", "tasks/t9"]),
            ],
            {},
            3600.0,
        ),
        ([ledger("back in scope", at(9), "tasks/t3")], {}, 3600.0),
        ([ledger("task open", at(8, 30), "tasks/t3")], {}, 5400.0),
        ([ledger("review changed", at(9), "phases/p1")], {}, 3600.0),
        ([ledger("planning changed", at(9), "phases/p1")], {}, 3600.0),
        ([ledger("review changed", at(9), "phases/p9")], {}, 10800.0),
        ([ledger("checked", at(9), "phases/p9")], {}, 10800.0),
    ],
)
def test_a_task_waits_from_the_latest_event_that_made_it_claimable(events, doc, seconds):
    assert waited(OPEN_TASK, *events, phases=PHASES, **doc) == {("seat_wait", ""): seconds}


def test_a_task_waits_from_the_check_that_opened_its_phase():
    task = {"id": "t3", "state": "open", "phase": "p2"}
    phases = [{"id": "p1", "done": True}, {"id": "p2", "depends_on": ["p1"]}]
    assert waited(task, ledger("checked", at(9), "phases/p1"), phases=phases) == {("seat_wait", ""): 3600.0}


def test_a_focus_on_the_lane_of_the_task_opens_it():
    task = {**OPEN_TASK, "lane": "eng"}
    focus = {"freezes": [{"verb": "focus", "target": "lane:eng"}]}
    assert waited(task, ledger("focused", at(8), "lane:eng"), phases=PHASES, **focus) == {("seat_wait", ""): 7200.0}


def test_a_task_waits_from_the_approval_of_its_planned_phase():
    task = {"id": "t3", "state": "open", "phase": "p1"}
    plan = {"id": "tp", "state": "done", "kind": "plan", "phase": "p1"}
    phases = [{"id": "p1", "planning": "auto", "review": {"state": "approved"}}]
    events = (ledger("task done", at(8), "tasks/tp"), ledger("review changed", at(9), "phases/p1"))
    assert waited(task, *events, phases=phases, tasks=[plan, task]) == {("seat_wait", ""): 3600.0}


def test_clearing_one_focus_while_another_still_excludes_the_task_does_not_open_it():
    task = {**OPEN_TASK, "state": "claimed"}
    events = [
        ledger("added", at(7), "tasks/t3"),
        ledger("focused", at(8), "tasks/t9"),
        ledger("focused", at(8, 30), "phases/p2"),
        opened("unfrozen", at(9), "tasks/t9", cleared=["tasks/t9"]),
        ledger("joined", at(9, 15), by="e1"),
    ]
    claim = [agent("claim", "t3", at(9, 15)), agent("spawn", "t3", at(9, 15), "e1", "claude")]
    ready = stages.claimable({"tasks": [task], "phases": [*PHASES, {"id": "p2"}]}, "")
    found = stages.report(rows(ledger_events=events, agent_events=claim), [task], NOW, ready=ready)
    assert seconds_of(found)[("seat_wait", "")] == 8100.0


def test_an_unfreeze_under_a_focus_that_still_excludes_the_task_does_not_open_it():
    task = {**OPEN_TASK, "state": "claimed"}
    events = [
        ledger("added", at(7), "tasks/t3"),
        ledger("focused", at(8), "phases/p2"),
        ledger("frozen", at(8, 30), "tasks/t3"),
        opened("unfrozen", at(9), "tasks/t3", cleared=["tasks/t3"]),
        ledger("joined", at(9, 15), by="e1"),
    ]
    claim = [agent("claim", "t3", at(9, 15)), agent("spawn", "t3", at(9, 15), "e1", "claude")]
    ready = stages.claimable({"tasks": [task], "phases": [*PHASES, {"id": "p2"}]}, "")
    found = stages.report(rows(ledger_events=events, agent_events=claim), [task], NOW, ready=ready)
    assert seconds_of(found)[("seat_wait", "")] == 8100.0


def lane_rows(added, *extra, claimed=at(5)):
    return rows(
        agent_events=[agent("claim", "t1", claimed), agent("spawn", "t1", claimed, "e1", "claude"), *extra],
        ledger_events=[
            ledger("joined", claimed, by="e1"),
            *(ledger("added", ts, f"tasks/{t}") for t, ts in added.items()),
        ],
    )


def lane_tasks(t2="eng", t3="eng", t1="eng"):
    return [
        {"id": "t1", "state": "claimed", "branch": "b1", "lane": t1},
        {"id": "t2", "state": "open", "lane": t2},
        {"id": "t3", "state": "open", "lane": t3},
    ]


def split(source, tasks, seats):
    return seconds_of(stages.report(source, tasks, NOW, ready=stages.claimable({"tasks": tasks}, "", seats)))


TWO_READY = {"t2": at(8), "t3": at(8)}


def test_only_as_many_ready_tasks_as_the_lane_has_seats_wait_for_a_seat_while_every_seat_is_busy():
    assert split(lane_rows(TWO_READY), lane_tasks(), {"eng": 1, "ci": 1}) == {
        ("build", ""): 14400.0,
        ("seat_wait", ""): 7200.0,
        ("queue_wait", ""): 7200.0,
    }


def test_ready_tasks_with_a_free_seat_are_queue_wait():
    assert split(lane_rows(TWO_READY), lane_tasks(), {"eng": 2, "ci": 1}) == {
        ("build", ""): 14400.0,
        ("queue_wait", ""): 14400.0,
    }


def test_a_lane_with_no_seats_holds_only_queue_wait():
    assert split(lane_rows(TWO_READY), lane_tasks(), {"eng": 0, "ci": 1}) == {
        ("build", ""): 14400.0,
        ("queue_wait", ""): 14400.0,
    }


def test_seat_wait_ends_when_the_busy_seat_frees():
    found = split(lane_rows(TWO_READY, agent("retire", "t1", at(9), "e1", "claude")), lane_tasks(), {"eng": 1})
    assert found == {("build", ""): 10800.0, ("seat_wait", ""): 3600.0, ("queue_wait", ""): 14400.0}


def test_the_earliest_ready_task_takes_the_seat_demand():
    found = split(lane_rows({"t2": at(8), "t3": at(7)}), lane_tasks(), {"eng": 1})
    assert found == {("build", ""): 14400.0, ("seat_wait", ""): 10800.0, ("queue_wait", ""): 7200.0}


def test_each_lane_counts_its_own_seats():
    found = split(lane_rows(TWO_READY), lane_tasks(t2="ci"), {"eng": 1, "ci": 1})
    assert found == {("build", ""): 14400.0, ("seat_wait", ""): 7200.0, ("queue_wait", ""): 7200.0}
    tasks = lane_tasks(t2="ci")
    ready = stages.claimable({"tasks": tasks}, "", {"eng": 1, "ci": 1})
    episodes = stages.observations(lane_rows(TWO_READY), tasks, NOW, 4 * H, ready)
    assert (episodes["seat_wait"], episodes["queue_wait"]) == ([f"seat_wait:t3:{at(8)}"], [f"queue_wait:t2:{at(8)}"])


def test_a_lane_missing_from_the_seat_caps_has_no_seats():
    found = split(lane_rows(TWO_READY), lane_tasks(t2="ci"), {"eng": 1})
    assert found == {("build", ""): 14400.0, ("seat_wait", ""): 7200.0, ("queue_wait", ""): 7200.0}
    assert split(lane_rows(TWO_READY), lane_tasks(t2="ci", t3="ci"), {"eng": 1}) == {
        ("build", ""): 14400.0,
        ("queue_wait", ""): 14400.0,
    }


def test_a_busy_seat_counts_only_in_the_lane_of_its_task():
    found = split(lane_rows(TWO_READY), lane_tasks(t1="ci"), {"eng": 1, "ci": 1})
    assert found == {("build", ""): 14400.0, ("queue_wait", ""): 14400.0}
    missing = split(lane_rows(TWO_READY), lane_tasks(t1="ci", t2="ci", t3="ci"), {"eng": 1})
    assert missing == {("build", ""): 14400.0, ("queue_wait", ""): 14400.0}


def test_a_task_without_a_lane_waits_in_the_engineer_lane():
    tasks = [{"id": "t1", "state": "claimed", "branch": "b1"}, {"id": "t2", "state": "open"}]
    found = split(lane_rows({"t2": at(8)}), tasks, {"eng": 1})
    assert found == {("build", ""): 14400.0, ("seat_wait", ""): 7200.0}


def test_seat_wait_starts_when_the_last_free_seat_is_taken():
    found = split(lane_rows(TWO_READY, claimed=at(9)), lane_tasks(), {"eng": 1})
    assert found == {("build", ""): 3600.0, ("seat_wait", ""): 3600.0, ("queue_wait", ""): 10800.0}


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
