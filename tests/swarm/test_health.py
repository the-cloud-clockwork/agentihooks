import pytest

from scripts.swarm.health import findings as health

NOW = 10_000_000_000
MIN = 60_000
LIMITS = health.Limits()
WORKER = "sw-eng-1"


def ev(kind, target="", by=WORKER, at=NOW - MIN):
    return {"kind": kind, "target": target, "by": by, "at": at}


def task(task_id, state="done", claimed_by=WORKER, pr_url="https://example.test/pull/1", **extra):
    return {"id": task_id, "state": state, "claimed_by": claimed_by, "pr_url": pr_url, **extra}


def agent(name=WORKER, lane="eng", task_id="t1", idle_ticks=0):
    return {"name": name, "lane": lane, "task": task_id, "idle_ticks": idle_ticks}


def healthy():
    events = [
        ev("added", "tasks/t1", by="sw-master-1"),
        ev("task claimed", "tasks/t1", by="swarm"),
        ev("joined"),
        ev("comment added", "phases/p1"),
        ev("comment edited", "phases/p1"),
        ev("task pr", "tasks/t1"),
        ev("task done", "tasks/t1"),
        ev("left"),
        ev("added", "tasks/t2", by="sw-master-1"),
        ev("task claimed", "tasks/t2", by="swarm"),
        ev("comment added", "phases/p1", by="sw-eng-2"),
    ]
    ledger = {
        "tasks": [task("t1"), task("t2", state="claimed", claimed_by="sw-eng-2", pr_url="")],
        "_meta": {"events": events},
    }
    agents = [agent("sw-master-1", "master", "master"), agent("sw-eng-2", task_id="t2", idle_ticks=1)]
    activity = {"sw-eng-2": {"watch": 6, "act": 9, "since": 0}, "sw-master-1": {"watch": 30, "act": 10, "since": 0}}
    return ledger, agents, activity


def run(ledger, agents=(), activity=None, limits=LIMITS):
    return [f.as_dict() for f in health.findings(ledger, list(agents), activity or {}, limits)]


def test_a_healthy_event_set_produces_no_finding():
    assert run(*healthy()) == []


def test_ceremony_names_the_agent_its_transitions_and_its_outcomes():
    events = [ev("comment edited", "phases/p1") for _ in range(25)] + [ev("task done", "tasks/t1")]
    ledger = {"tasks": [task("t1", claimed_by="sw-eng-2")], "_meta": {"events": events}}
    assert run(ledger) == [
        {
            "kind": "ceremony",
            "subject": WORKER,
            "summary": "more ledger transitions than outcomes",
            "evidence": ["26 ledger transitions", "1 outcome"],
            "threshold": "at least 20 transitions and more than 12 per outcome",
        }
    ]


def test_the_master_is_credited_with_every_merged_task():
    events = [ev("comment edited", "phases/p1", by="sw-master-1") for _ in range(24)]
    events += [ev("task done", f"tasks/t{n}") for n in range(2)]
    ledger = {"tasks": [task("t0"), task("t1")], "_meta": {"events": events}}
    assert run(ledger) == []


def ops_task(task_id, proof):
    return task(task_id, pr_url="", kind="ops", proof=proof)


def test_ops_tasks_closed_with_their_proof_are_outcomes():
    proof = {"command": "systemctl status sync", "output": "active (running)"}
    events = [ev("comment edited", "phases/p1") for _ in range(24)]
    events += [ev("comment edited", "phases/p1", by="sw-master-1") for _ in range(30)]
    events += [ev("task done", f"tasks/o{n}") for n in range(3)]
    ledger = {"tasks": [ops_task(f"o{n}", proof) for n in range(3)], "_meta": {"events": events}}
    assert run(ledger) == []


def test_transitions_with_neither_merges_nor_proofs_are_still_ceremony():
    events = [ev("comment edited", "phases/p1") for _ in range(24)]
    events += [ev("task done", f"tasks/{tid}") for tid in ("c0", "o0")]
    tasks = [task("c0", pr_url=""), ops_task("o0", {"command": "systemctl status sync"})]
    assert run({"tasks": tasks, "_meta": {"events": events}}) == [
        {
            "kind": "ceremony",
            "subject": WORKER,
            "summary": "more ledger transitions than outcomes",
            "evidence": ["26 ledger transitions", "0 outcomes"],
            "threshold": "at least 20 transitions and more than 12 per outcome",
        }
    ]


def test_an_agent_whose_open_pull_request_is_green_raises_no_ceremony_finding():
    events = [ev("comment edited", "phases/p1") for _ in range(25)]
    ledger = {"tasks": [task("t1", state="pr")], "_meta": {"events": events}}
    found = health.findings(ledger, [], {}, LIMITS, green={"t1", "gone"})
    assert [f.as_dict() for f in found] == []


def test_a_green_pull_request_credits_only_the_agent_that_claimed_it():
    events = [ev("comment edited", "phases/p1", by="sw-eng-2") for _ in range(25)]
    ledger = {"tasks": [task("t1", state="pr")], "_meta": {"events": events}}
    found = health.findings(ledger, [], {}, LIMITS, green={"t1"})
    assert [(f.subject, f.evidence) for f in found] == [("sw-eng-2", ("25 ledger transitions", "0 outcomes"))]


def test_an_agent_whose_claimed_task_closed_with_its_outcome_raises_no_ceremony_finding():
    events = [ev("comment edited", "phases/p1") for _ in range(25)] + [ev("task done", "tasks/t1")]
    ledger = {"tasks": [task("t1")], "_meta": {"events": events}}
    assert run(ledger) == []


def test_a_claimed_task_closed_without_its_outcome_is_still_ceremony():
    events = [ev("comment edited", "phases/p1") for _ in range(25)] + [ev("task done", "tasks/t1")]
    ledger = {"tasks": [task("t1", pr_url="")], "_meta": {"events": events}}
    assert [f["evidence"] for f in run(ledger)] == [["26 ledger transitions", "0 outcomes"]]


def test_an_open_pull_request_without_green_checks_is_still_ceremony():
    events = [ev("comment edited", "phases/p1") for _ in range(25)]
    ledger = {"tasks": [task("t1", state="pr")], "_meta": {"events": events}}
    assert [f["evidence"] for f in run(ledger)] == [["25 ledger transitions", "0 outcomes"]]


DISPATCHER = "dispatcher@323133-0001"
ENGINEER = "engineer@323133-0002"


def settled(by, items=24, decided=True):
    events = []
    for n in range(items):
        target = f"followups/f{n}"
        events += [ev("comment added", target, by=by)] if decided else [ev("comment edited", "phases/p1", by=by)]
        events.append(ev("checked", target, by=by))
    return events


def cleared(by, n):
    return ev("priority cleared", f"followups/c{n}", by=by)


def test_a_dispatcher_seat_is_credited_with_each_priority_it_settles_with_a_decision():
    events = settled(DISPATCHER) + [cleared(DISPATCHER, n) for n in range(5)]
    assert run({"tasks": [], "_meta": {"events": events}}) == []


def test_the_dispatcher_seat_that_settled_twenty_four_priorities_no_longer_trips_ceremony():
    events = [ev("joined", by=DISPATCHER), *settled(DISPATCHER)]
    events += [ev("comment added", f"tasks/t{n}", by=DISPATCHER) for n in range(2)]
    events += [cleared(DISPATCHER, n) for n in range(5)] + [ev("left", by=DISPATCHER)]
    moves = [e for e in events if e["kind"] not in health.NOT_TRANSITIONS]
    assert (len(moves), health._decided(events)[DISPATCHER]) == (55, 24)
    assert run({"tasks": [], "_meta": {"events": events}}) == []


def test_an_engineer_seat_with_the_same_settles_is_still_ceremony():
    events = settled(ENGINEER) + [cleared(ENGINEER, n) for n in range(5)]
    assert [(f["subject"], f["evidence"]) for f in run({"tasks": [], "_meta": {"events": events}})] == [
        (ENGINEER, ["53 ledger transitions", "0 outcomes"])
    ]


def test_a_dispatcher_seat_settling_without_a_decision_is_still_ceremony():
    events = settled(DISPATCHER, decided=False) + [cleared(DISPATCHER, n) for n in range(5)]
    assert [(f["subject"], f["evidence"]) for f in run({"tasks": [], "_meta": {"events": events}})] == [
        (DISPATCHER, ["53 ledger transitions", "0 outcomes"])
    ]


def test_a_priority_cleared_after_the_seat_comment_counts_once_per_item():
    events = [ev("comment edited", "phases/p1", by=DISPATCHER) for _ in range(22)]
    events += [ev("comment added", "followups/c0", by=DISPATCHER), cleared(DISPATCHER, 0)]
    events += [cleared(DISPATCHER, 0), cleared(DISPATCHER, 1)]
    assert [f["evidence"] for f in run({"tasks": [], "_meta": {"events": events}})] == [
        ["26 ledger transitions", "1 outcome"]
    ]


def test_a_dispatcher_seat_counts_a_closed_task_as_a_second_outcome():
    events = [ev("comment edited", "phases/p1", by=DISPATCHER) for _ in range(24)]
    events += [ev("comment added", "followups/f0", by=DISPATCHER), ev("checked", "followups/f0", by=DISPATCHER)]
    events.append(ev("task done", "tasks/t1", by=DISPATCHER))
    assert [f["evidence"] for f in run({"tasks": [task("t1")], "_meta": {"events": events}})] == [
        ["27 ledger transitions", "2 outcomes"]
    ]


def test_a_comment_by_another_agent_is_not_the_dispatcher_decision():
    events = [ev("comment added", f"followups/f{n}", by=ENGINEER) for n in range(3)]
    events += [ev("checked", f"followups/f{n}", by=DISPATCHER) for n in range(3)]
    events += [ev("comment edited", "phases/p1", by=DISPATCHER) for _ in range(20)]
    assert [(f["subject"], f["evidence"]) for f in run({"tasks": [], "_meta": {"events": events}})] == [
        (DISPATCHER, ["23 ledger transitions", "0 outcomes"])
    ]


def test_scope_inflation_lists_each_task_by_title_with_its_gain():
    events = [ev("added", f"tasks/q{n}") for n in range(3)]
    tasks = [
        task("q0", state="open", gain=4, title="Split the parser"),
        task("q1", state="open", gain=1.5, title="Cache the index"),
        task("q2", state="open"),
    ]
    assert run({"tasks": tasks, "_meta": {"events": events}}) == [
        {
            "kind": "scope inflation",
            "subject": WORKER,
            "summary": "queued 3 tasks for its own lane",
            "evidence": ["Split the parser, gain 4", "Cache the index, gain 1.5", "q2, no gain stated"],
            "threshold": "3 self queued tasks whose gain never rose",
        }
    ]


def test_rising_gain_or_master_queued_tasks_are_not_inflation():
    events = [ev("added", f"tasks/q{n}") for n in range(3)] + [
        ev("added", f"tasks/m{n}", by="sw-master-1") for n in range(5)
    ]
    tasks = [task(f"q{n}", state="open", gain=n + 1) for n in range(3)] + [
        task(f"m{n}", state="open") for n in range(5)
    ]
    assert run({"tasks": tasks, "_meta": {"events": events}}) == []


def test_proof_loop_names_the_task_its_reruns_and_review_rounds():
    events = [ev("task started", "tasks/t1", by="swarm") for _ in range(4)] + [ev("task pr", "tasks/t1")]
    assert run({"tasks": [task("t1")], "_meta": {"events": events}}) == [
        {
            "kind": "proof loop",
            "subject": "t1",
            "summary": "reruns or review rounds over the cap",
            "evidence": ["task t1", "started 4 times (3 reruns)", "1 review round"],
            "threshold": "more than 2 reruns or 3 review rounds",
        }
    ]


def test_claims_without_started_lives_raise_no_proof_loop():
    events = [ev("task claimed", "tasks/t1", by="swarm") for _ in range(5)]
    assert run({"tasks": [task("t1")], "_meta": {"events": events}}) == []


def test_repeated_failed_launches_measure_their_count():
    events = [{"kind": "launch failed", "target": "tasks/t1", "error": "canary timeout"} for _ in range(3)]
    found = health.failed_launches(events, {"t1": {"id": "t1", "title": "Fix"}})
    assert [(f.subject, f.measure) for f in found] == [("t1", 3)]


def test_failed_launch_findings_require_repetition_and_keep_each_launch_error():
    first = {**ev("launch failed", "tasks/t1", by="swarm"), "error": "profile canary timeout"}
    second = {**ev("launch failed", "tasks/t1", by="swarm"), "error": "worktree timer"}
    doc = {"tasks": [task("t1")], "_meta": {"events": [first]}}
    assert run(doc) == []
    doc["_meta"]["events"].append(second)
    assert run(doc) == [
        {
            "kind": "failed launch",
            "subject": "t1",
            "summary": "repeated launches failed before an agent life started",
            "evidence": ["task t1", "2 failed launches", "profile canary timeout", "worktree timer"],
            "threshold": "at least 2 failed launches",
        }
    ]


def test_review_rounds_over_the_cap_are_a_proof_loop():
    events = [ev("task started", "tasks/t1", by="swarm")] + [ev("task pr", "tasks/t1") for _ in range(4)]
    found = run({"tasks": [task("t1")], "_meta": {"events": events}})
    assert [(f["kind"], f["evidence"]) for f in found] == [
        ("proof loop", ["task t1", "started 1 time (0 reruns)", "4 review rounds"])
    ]


def test_an_idle_agent_holding_a_claim_is_named_with_its_task():
    ledger = {
        "tasks": [task("t1", state="claimed", pr_url="", title="Fold the chat panel")],
        "_meta": {"events": [ev("comment added", "phases/p1")]},
    }
    assert run(ledger, [agent(idle_ticks=4)]) == [
        {
            "kind": "idle with claim",
            "subject": WORKER,
            "summary": "idle for 4 ticks while holding a task",
            "evidence": ["task Fold the chat panel (claimed)"],
            "threshold": "3 idle ticks",
        }
    ]


def test_an_agent_waiting_on_checks_does_not_trip_idle_with_claim():
    ledger = {"tasks": [task("t1", state="pr", title="Fold the chat panel")], "_meta": {"events": []}}
    idle = [agent(idle_ticks=5)]
    assert health.findings(ledger, idle, {}, LIMITS, waiting={"t1"}) == []
    assert [f.kind for f in health.findings(ledger, idle, {}, LIMITS)] == ["idle with claim"]


def test_each_finding_carries_a_stable_id_and_the_measure_its_evidence_grows_by():
    ledger = {"tasks": [task("t1", state="claimed", pr_url="")], "_meta": {"events": []}}
    [idle] = health.findings(ledger, [agent(idle_ticks=4)], {}, LIMITS)
    [watch] = health.over_monitoring({"sw-eng-1": {"watch": 40, "act": 2, "since": 33}}, LIMITS)
    assert (idle.id, idle.measure) == ("idle-with-claim/sw-eng-1", 4)
    assert (watch.id, watch.measure) == ("over-monitoring/sw-eng-1", 33)
    assert "measure" not in watch.as_dict()


def test_a_quiet_claim_names_the_task_its_holder_and_the_quiet_time():
    ledger = {"tasks": [task("t1", state="claimed", pr_url="", title="Fold the chat panel")], "_meta": {"events": []}}
    assert run(ledger, [{**agent(), "quiet_minutes": 44}]) == [
        {
            "kind": "stale claim",
            "subject": "t1",
            "summary": "no progress for 44 minutes",
            "evidence": ["task Fold the chat panel", f"claimed by {WORKER}"],
            "threshold": "30 minutes without progress",
        }
    ]
    [found] = health.findings(ledger, [{**agent(), "quiet_minutes": 30}], {}, LIMITS)
    assert (found.id, found.measure) == ("stale-claim/t1", 30)


def test_a_claim_quiet_under_the_limit_or_unmeasured_is_not_stale():
    ledger = {"tasks": [task("t1", state="claimed", pr_url="")], "_meta": {"events": []}}
    assert run(ledger, [{**agent(), "quiet_minutes": 29}]) == []
    assert run(ledger, [agent()]) == []


def test_busy_agent_with_stale_tool_activity_is_reported_stalled():
    ledger = {"tasks": [task("t1", state="claimed")]}
    busy = {**agent(), "pane_state": "working", "tool_quiet_minutes": 10}
    found = health.findings(ledger, [busy], {}, LIMITS)
    assert [f.kind for f in found] == ["stalled"]
    assert found[0].subject == WORKER
    assert found[0].measure == 10
    assert found[0].summary == "busy with no tool call for 10 minutes"
    assert found[0].threshold == "10 minutes without a tool call"
    assert found[0].evidence == ("pane working; task t1",)


def test_stalled_busy_threshold_is_configurable_and_does_not_flag_other_panes():
    limits = health.limits({"AGENTIHOOKS_HEALTH_STALLED_MINUTES": "4"})
    ledger = {"tasks": [task("t1", state="claimed")]}
    busy = {**agent(), "pane_state": "working", "tool_quiet_minutes": 4}
    assert [f.kind for f in health.findings(ledger, [busy], {}, limits)] == ["stalled"]
    for row in (
        {**busy, "tool_quiet_minutes": 3},
        {**busy, "tool_quiet_minutes": None},
        {**busy, "pane_state": "idle"},
        {**busy, "pane_state": "waiting"},
    ):
        assert health.findings(ledger, [row], {}, limits) == []


def test_fresh_agent_does_not_hide_a_stalled_agent_and_taskless_evidence_stays_named():
    fresh = {**agent("fresh"), "pane_state": "working", "tool_quiet_minutes": 1}
    busy = {"name": "busy", "pane_state": "working", "tool_quiet_minutes": 10}
    found = health.stalled([fresh, busy], LIMITS)
    assert [f.subject for f in found] == ["busy"]
    assert found[0].evidence == ("pane working; task ",)


def test_over_monitoring_names_the_agent_and_its_counts():
    activity = {WORKER: {"watch": 40, "act": 3, "since": 21}}
    assert run({"tasks": [], "_meta": {"events": []}}, activity=activity) == [
        {
            "kind": "over monitoring",
            "subject": WORKER,
            "summary": "more watch calls than actions",
            "evidence": ["21 watch calls since the last action", "40 watch calls", "3 actions"],
            "threshold": "more than 20 watch calls since the last action and more than 5 per action",
        }
    ]


def test_an_agent_the_watch_gate_held_at_its_budget_gets_no_finding():
    activity = {WORKER: {"watch": 40, "act": 3, "since": 20}}
    assert run({"tasks": [], "_meta": {"events": []}}, activity=activity) == []


def test_watching_after_the_last_action_within_the_ratio_gets_no_finding():
    activity = {WORKER: {"watch": 25, "act": 5, "since": 25}}
    assert run({"tasks": [], "_meta": {"events": []}}, activity=activity) == []


def test_the_master_gets_a_looser_over_monitoring_limit_than_other_agents():
    counts = {"watch": 40, "act": 4, "since": 40}
    found = run({"tasks": [], "_meta": {"events": []}}, activity={"sw-master-1": counts, WORKER: counts})
    assert [f["subject"] for f in found] == [WORKER]


def test_a_master_past_its_looser_limit_still_gets_a_finding():
    found = run({"tasks": [], "_meta": {"events": []}}, activity={"sw-master-1": {"watch": 70, "act": 3, "since": 61}})
    assert found == [
        {
            "kind": "over monitoring",
            "subject": "sw-master-1",
            "summary": "more watch calls than actions",
            "evidence": ["61 watch calls since the last action", "70 watch calls", "3 actions"],
            "threshold": "more than 60 watch calls since the last action and more than 15 per action",
        }
    ]


@pytest.mark.parametrize(("by", "least", "ratio"), [("sw-eng-1", 20, 5), ("sw-ci-1", 20, 5), ("sw-master-1", 60, 15)])
def test_watch_limits_pick_the_master_pair_only_for_the_master(by, least, ratio):
    assert health.watch_limits(by, LIMITS) == (least, ratio)


@pytest.mark.parametrize(
    ("counts", "over"),
    [
        ({"watch": 21, "act": 0, "since": 21}, True),
        ({"watch": 20, "act": 0, "since": 20}, False),
        ({"watch": 30, "act": 6, "since": 21}, False),
        ({"watch": 31, "act": 6, "since": 21}, True),
        ({"watch": 6, "act": 1, "since": 21}, True),
    ],
)
def test_over_watched_needs_both_the_since_count_and_the_ratio(counts, over):
    assert health.over_watched(counts, 20, 5) is over


def test_master_watch_limits_come_from_the_environment():
    limits = health.limits({"AGENTIHOOKS_HEALTH_MASTER_WATCH_MIN": "90", "AGENTIHOOKS_HEALTH_MASTER_WATCH_RATIO": "30"})
    assert (limits.master_watch_min, limits.master_watch_ratio) == (90, 30)
    assert (limits.watch_min, limits.watch_ratio) == (LIMITS.watch_min, LIMITS.watch_ratio)


def test_limits_come_from_the_environment_and_fall_back_on_bad_values():
    limits = health.limits({"AGENTIHOOKS_HEALTH_STALE_MINUTES": "5", "AGENTIHOOKS_HEALTH_WATCH_RATIO": "x"})
    assert (limits.stale_minutes, limits.watch_ratio) == (5, LIMITS.watch_ratio)
    assert health.limits({}) == LIMITS


@pytest.mark.parametrize(
    "name,value",
    [
        ("ceremony_min", 20),
        ("ceremony_ratio", 12),
        ("self_queued", 3),
        ("reruns", 2),
        ("review_rounds", 3),
        ("idle_ticks", 3),
        ("stale_minutes", 30),
        ("watch_min", 20),
        ("watch_ratio", 5),
        ("master_watch_min", 60),
        ("master_watch_ratio", 15),
        ("cooldown_minutes", 60),
        ("drain_minutes", 10),
        ("drain_left", 10),
    ],
)
def test_defaults_match_the_toolbelt_rule(name, value):
    assert getattr(LIMITS, name) == value
