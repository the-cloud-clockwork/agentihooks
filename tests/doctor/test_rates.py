from scripts.doctor import rates
from scripts.doctor.rates import Pull, Records, Window

MIN = 60_000
ENG, MASTER = "sw-eng-1", "sw-master-1"
WIN = Window(100 * MIN, 200 * MIN)
URL = "https://github.com/o/r/pull/{}"


def ev(at, kind, target="", by=ENG):
    return {"at": at * MIN, "kind": kind, "target": target, "by": by}


def recs(**parts):
    return Records(
        events=parts.get("events", []),
        tasks=parts.get("tasks", {}),
        activity=parts.get("activity", {}),
        findings=parts.get("findings", {}),
        gate_log=parts.get("gate_log", []),
        injections=parts.get("injections", []),
        corrections=parts.get("corrections", []),
        pulls=parts.get("pulls", {}),
    )


def test_a_window_holds_its_start_and_not_its_end():
    assert WIN.holds(100 * MIN) and WIN.holds(200 * MIN - 1)
    assert not WIN.holds(100 * MIN - 1) and not WIN.holds(200 * MIN)


def test_ratio_is_none_without_a_denominator_and_rounded_otherwise():
    assert rates.ratio(5, 0) is None
    assert rates.ratio(0, 3) == 0
    assert rates.ratio(2, 3) == 0.67
    assert rates._average([1, 2, 2]) == 1.67
    assert rates._average([]) is None


def test_a_task_event_names_its_task_and_any_other_target_names_none():
    assert rates._task({"target": "tasks/t1"}) == "t1"
    assert rates._task({"target": "phases/p1"}) == ""


def test_ceremony_counts_worker_talk_in_the_window_per_outcome():
    events = [
        ev(110, "comment added", "phases/p1"),
        ev(111, "comment edited", "phases/p1"),
        ev(112, "message added", "chat"),
        ev(113, "added", "followups/f1"),
        ev(114, "added", "tasks/t9"),
        ev(115, "artifact added", "artifacts/a"),
        ev(116, "comment added", "phases/p1", by=MASTER),
        ev(117, "comment added", "phases/p1", by="operator"),
        ev(118, "comment added", "phases/p1", by="ci@323133-0007"),
        ev(99, "comment added", "phases/p1"),
        ev(200, "comment added", "phases/p1"),
        ev(120, "task pr", "tasks/t1", by="swarm"),
        ev(130, "task done", "tasks/t1", by="swarm"),
        ev(131, "task claimed", "tasks/t2", by="swarm"),
    ]
    assert rates.ceremony(recs(events=events), WIN) == {"talk writes": 5, "outcomes": 2, "talk per outcome": 2.5}


def test_planted_fault_one_more_worker_comment_raises_the_ceremony_rate():
    events = [ev(110, "comment added", "phases/p1"), ev(120, "task done", "tasks/t1", by="swarm")]
    before = rates.ceremony(recs(events=events), WIN)["talk per outcome"]
    after = rates.ceremony(recs(events=[*events, ev(121, "comment added", "phases/p1")]), WIN)["talk per outcome"]
    assert (before, after) == (1.0, 2.0)


def merged(n, at, files=(), lines=10, state="MERGED"):
    return Pull(state, at * MIN if state == "MERGED" else None, lines, tuple(files))


def test_scope_inflation_reads_merged_pull_requests_of_tasks_done_in_the_window():
    tasks = {
        "t1": {"id": "t1", "pr_url": URL.format(1), "territory": ["scripts/doctor"]},
        "t2": {"id": "t2", "pr_url": URL.format(2), "territory": []},
        "t3": {"id": "t3", "pr_url": URL.format(3), "territory": ["a"]},
        "t4": {"id": "t4", "pr_url": URL.format(4), "territory": ["a"]},
        "t5": {"id": "t5", "pr_url": URL.format(5), "territory": ["hooks/", "BOX/"]},
        "t6": {"id": "t6", "pr_url": URL.format(6), "territory": ["a"]},
    }
    pulls = {
        URL.format(1): merged(1, 140, ("scripts/doctor/rates.py", "tests/doctor/test_rates.py", "README.md"), 30),
        URL.format(2): merged(2, 140, ("x.py",), 50),
        URL.format(3): merged(3, 140, ("b.py",), 1000, state="OPEN"),
        URL.format(4): merged(4, 140, ("b.py",), 1000),
        URL.format(5): merged(5, 140, ("hooks/a.py", "BOX/b.py"), 10),
    }
    events = [
        ev(150, "task done", "tasks/t1", by="swarm"),
        ev(150, "task done", "tasks/t2", by="swarm"),
        ev(150, "task done", "tasks/t3", by="swarm"),
        ev(50, "task done", "tasks/t4", by="swarm"),
        ev(150, "task done", "tasks/t5", by="swarm"),
        ev(150, "task done", "tasks/t6", by="swarm"),
        ev(160, "added", "tasks/t7"),
        ev(161, "added", "tasks/t8", by=MASTER),
        ev(162, "added", "followups/f1"),
    ]
    got = rates.scope_inflation(recs(events=events, tasks=tasks, pulls=pulls), WIN)
    assert got == {
        "merged tasks": 3,
        "lines per merged task": 30.0,
        "files outside territory per merged task": 1.0,
        "tasks added by workers": 1,
    }


def test_context_narrowing_counts_reopens_of_tasks_that_reached_pr_or_done():
    events = [
        ev(10, "task pr", "tasks/t1", by="swarm"),
        ev(120, "task open", "tasks/t1", by=MASTER),
        ev(121, "task open", "tasks/t2", by=MASTER),
        ev(130, "task done", "tasks/t3", by="swarm"),
        ev(131, "task done", "tasks/t4", by="swarm"),
        ev(140, "task open", "tasks/t3", by=MASTER),
        ev(205, "task open", "tasks/t4", by=MASTER),
    ]
    assert rates.context_narrowing(recs(events=events), WIN) == {
        "tasks reopened": 2,
        "tasks done": 2,
        "reopened per done": 1.0,
    }


def test_proof_loops_count_claims_and_pr_moves_per_task():
    events = [
        ev(110, "task claimed", "tasks/t1", by="swarm"),
        ev(120, "task claimed", "tasks/t1", by="swarm"),
        ev(130, "task claimed", "tasks/t2", by="swarm"),
        ev(140, "task pr", "tasks/t1", by="swarm"),
        ev(141, "task pr", "tasks/t1", by="swarm"),
        ev(142, "task pr", "tasks/t1", by="swarm"),
        ev(99, "task claimed", "tasks/t3", by="swarm"),
    ]
    assert rates.proof_loops(recs(events=events), WIN) == {
        "tasks claimed": 2,
        "claims per task": 1.5,
        "review rounds per task": 1.5,
        "CI reruns per task": 0.0,
        "sub-agent calls per task": 0.0,
    }


def test_inherited_rules_count_injections_after_a_correction_and_sources_corrected_twice():
    corrections = [
        {"at": 105 * MIN, "source": "e1"},
        {"at": 150 * MIN, "source": "e1"},
        {"at": 5 * MIN, "source": "b"},
        {"at": 10 * MIN, "source": "z"},
        {"at": 200 * MIN, "source": "z"},
    ]
    injections = [
        {"at": 101 * MIN, "source": "e1"},
        {"at": 106 * MIN, "source": "e1"},
        {"at": 120 * MIN, "source": "b"},
        {"at": 120 * MIN, "source": "other"},
        {"at": 250 * MIN, "source": "e1"},
    ]
    assert rates.inherited_rules(recs(corrections=corrections, injections=injections), WIN) == {
        "corrections": 2,
        "injections after correction": 2,
        "sources corrected twice": 1,
    }


def rows(*pairs):
    return [{"kind": kind, "at": at * MIN} for kind, at in pairs]


def test_monitoring_tallies_watch_per_action_for_workers_and_the_master_in_the_window():
    activity = {
        "not-an-agent": rows(("watch", 140)),
        ENG: [*rows(("watch", 110), ("watch", 111), ("watch", 112), ("act", 113), ("watch", 50), ("act", 250)), {}],
        "ci@323133-0007": rows(("watch", 120), ("act", 121)),
        MASTER: rows(("watch", 130), ("watch", 131), ("act", 132)),
    }
    assert rates.monitoring(recs(activity=activity), WIN) == {
        "worker watch calls": 4,
        "worker actions": 2,
        "worker watch per action": 2.0,
        "master watch per action": 2.0,
    }
    assert rates.monitoring(recs(activity={MASTER: rows(("watch", 130))}), WIN)["master watch per action"] is None


def finding(kind, at, measure):
    return {"seen_at": at * MIN, "measure": measure, "kind": kind}


def test_idle_counts_findings_first_seen_in_the_window_and_minutes_from_merge_to_done():
    findings = {
        "idle-with-claim/a": finding("idle with claim", 110, 3),
        "idle-with-claim/b": finding("idle with claim", 90, 4),
        "stale-claim/t1": finding("stale claim", 120, 40),
    }
    tasks = {
        "t1": {"id": "t1", "pr_url": URL.format(1)},
        "t2": {"id": "t2", "pr_url": URL.format(2)},
        "t3": {"id": "t3", "pr_url": URL.format(3)},
    }
    pulls = {URL.format(1): merged(1, 120), URL.format(2): merged(2, 100)}
    events = [
        ev(150, "task done", "tasks/t1", by="swarm"),
        ev(110, "task done", "tasks/t2", by="swarm"),
        ev(160, "task done", "tasks/t3", by="swarm"),
    ]
    got = rates.idle(recs(findings=findings, tasks=tasks, pulls=pulls, events=events), WIN)
    assert got == {"idle findings": 1, "minutes from merge to done": 20.0}


def test_stale_counts_findings_and_their_quiet_minutes():
    findings = {
        "stale-claim/t1": finding("stale claim", 120, 40),
        "stale-claim/t2": finding("stale claim", 130, 60),
        "stale-claim/t3": finding("stale claim", 10, 90),
        "idle-with-claim/a": finding("idle with claim", 110, 3),
    }
    assert rates.stale(recs(findings=findings), WIN) == {"stale findings": 2, "quiet minutes per finding": 50.0}


def test_premature_completion_counts_code_tasks_done_without_a_merged_pull_request():
    tasks = {
        "t1": {"id": "t1", "pr_url": URL.format(1)},
        "t2": {"id": "t2", "pr_url": URL.format(2), "kind": "ci"},
        "t3": {"id": "t3", "pr_url": ""},
        "t4": {"id": "t4", "pr_url": URL.format(4)},
        "t5": {"id": "t5", "kind": "ops"},
    }
    pulls = {URL.format(1): merged(1, 120), URL.format(2): merged(2, 120, state="CLOSED")}
    events = [ev(150, "task done", f"tasks/t{n}", by="swarm") for n in range(1, 6)]
    assert rates.premature(recs(tasks=tasks, pulls=pulls, events=events), WIN) == {
        "code tasks done": 4,
        "done without a merged pull request": 2,
        "pull requests unread": 1,
    }


def test_gate_counts_group_the_gate_log_by_gate_and_kind_in_the_window():
    log = [
        {"gate": "talk", "kind": "deny", "at": 110 * MIN},
        {"gate": "talk", "kind": "deny", "at": 111 * MIN},
        {"gate": "talk", "kind": "lift", "at": 112 * MIN},
        {"gate": "watch", "kind": "fail-open", "at": 113 * MIN},
        {"gate": "watch", "kind": "deny", "at": 300 * MIN},
        {"gate": "watch", "kind": "shrug", "at": 114 * MIN},
        {"gate": "talk", "kind": "deny"},
        {"kind": "deny", "at": 115 * MIN},
        {"gate": "talk", "at": 116 * MIN},
    ]
    assert rates.gate_counts(recs(gate_log=log), WIN) == {
        "talk": {"deny": 2, "observe": 0, "lift": 1, "fail-open": 0, "count": 0},
        "watch": {"deny": 0, "observe": 0, "lift": 0, "fail-open": 1, "count": 0},
    }


def test_proof_loops_count_ci_reruns_and_sub_agent_calls_per_task_from_count_rows():
    events = [ev(110, "task claimed", "tasks/t1", by="swarm"), ev(120, "task claimed", "tasks/t2", by="swarm")]
    log = [
        {"gate": "reruns", "kind": "count", "task": "t1", "at": 111 * MIN},
        {"gate": "reruns", "kind": "count", "task": "t1", "at": 112 * MIN},
        {"gate": "reruns", "kind": "count", "task": "t2", "at": 113 * MIN},
        {"gate": "reruns", "kind": "deny", "task": "t2", "at": 114 * MIN},
        {"gate": "reruns", "kind": "count", "task": "t2", "at": 300 * MIN},
        {"gate": "subagents", "kind": "count", "task": "t1", "at": 115 * MIN},
        {"gate": "subagents", "kind": "count", "task": "t1", "at": 116 * MIN},
        {"gate": "subagents", "kind": "observe", "task": "t1", "at": 117 * MIN},
        {"gate": "talk", "kind": "count", "task": "t1", "at": 118 * MIN},
        {"gate": "reruns", "kind": "count"},
    ]
    found = rates.proof_loops(recs(events=events, gate_log=log), WIN)
    assert (found["CI reruns per task"], found["sub-agent calls per task"]) == (1.5, 1.0)


def test_count_rows_are_a_gate_kind():
    log = [{"gate": "reruns", "kind": "count", "at": 110 * MIN}]
    assert rates.gate_counts(recs(gate_log=log), WIN) == {
        "reruns": {"deny": 0, "observe": 0, "lift": 0, "fail-open": 0, "count": 1}
    }


def test_rates_names_every_coordination_failure():
    assert list(rates.rates(recs(), WIN)) == [
        "ceremony",
        "scope inflation",
        "context narrowing",
        "proof loops",
        "inherited bad rules",
        "hyper monitoring",
        "idle with claim",
        "stale claim",
        "premature completion",
    ]
