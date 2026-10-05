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
    activity = {"sw-eng-2": {"watch": 6, "act": 9}, "sw-master-1": {"watch": 30, "act": 10}}
    return ledger, agents, activity


def run(ledger, agents=(), activity=None, limits=LIMITS):
    return [f.as_dict() for f in health.findings(ledger, list(agents), activity or {}, NOW, limits)]


def test_a_healthy_event_set_produces_no_finding():
    assert run(*healthy()) == []


def test_ceremony_names_the_agent_its_transitions_and_its_outcomes():
    events = [ev("comment edited", "phases/p1") for _ in range(25)] + [ev("task done", "tasks/t1")]
    ledger = {"tasks": [task("t1")], "_meta": {"events": events}}
    assert run(ledger) == [
        {
            "kind": "ceremony",
            "subject": WORKER,
            "evidence": "26 ledger transitions against 1 merged outcome",
            "threshold": "at least 20 transitions and more than 12 per outcome",
        }
    ]


def test_the_master_is_credited_with_every_merged_task():
    events = [ev("comment edited", "phases/p1", by="sw-master-1") for _ in range(24)]
    events += [ev("task done", f"tasks/t{n}") for n in range(2)]
    ledger = {"tasks": [task("t0"), task("t1")], "_meta": {"events": events}}
    assert run(ledger) == []


def test_scope_inflation_names_the_agent_and_each_gain():
    events = [ev("added", f"tasks/q{n}") for n in range(3)]
    tasks = [task("q0", state="open", gain=4), task("q1", state="open", gain=1.5), task("q2", state="open")]
    assert run({"tasks": tasks, "_meta": {"events": events}}) == [
        {
            "kind": "scope inflation",
            "subject": WORKER,
            "evidence": "queued 3 tasks for its own lane: q0 gain 4, q1 gain 1.5, q2 no gain stated",
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
    events = [ev("task claimed", "tasks/t1", by="swarm") for _ in range(4)] + [ev("task pr", "tasks/t1")]
    assert run({"tasks": [task("t1")], "_meta": {"events": events}}) == [
        {
            "kind": "proof loop",
            "subject": "t1",
            "evidence": "claimed 4 times (3 reruns), 1 review round",
            "threshold": "more than 2 reruns or 3 review rounds",
        }
    ]


def test_review_rounds_over_the_cap_are_a_proof_loop():
    events = [ev("task claimed", "tasks/t1", by="swarm")] + [ev("task pr", "tasks/t1") for _ in range(4)]
    found = run({"tasks": [task("t1")], "_meta": {"events": events}})
    assert [(f["kind"], f["evidence"]) for f in found] == [("proof loop", "claimed 1 time (0 reruns), 4 review rounds")]


def test_an_idle_agent_holding_a_claim_is_named_with_its_task():
    ledger = {
        "tasks": [task("t1", state="claimed", pr_url="")],
        "_meta": {"events": [ev("comment added", "phases/p1")]},
    }
    assert run(ledger, [agent(idle_ticks=4)]) == [
        {
            "kind": "idle with claim",
            "subject": WORKER,
            "evidence": "idle for 4 ticks while holding task t1 (claimed)",
            "threshold": "3 idle ticks",
        }
    ]


def test_a_claim_with_no_change_names_the_task_its_holder_and_the_quiet_time():
    events = [ev("task claimed", "tasks/t1", by="swarm", at=NOW - 45 * MIN), ev("joined", at=NOW - 44 * MIN)]
    ledger = {"tasks": [task("t1", state="claimed", pr_url="")], "_meta": {"events": events}}
    assert run(ledger) == [
        {
            "kind": "stale claim",
            "subject": "t1",
            "evidence": f"claimed by {WORKER}, no change for 44 minutes",
            "threshold": "30 minutes without a change",
        }
    ]


def test_over_monitoring_names_the_agent_and_both_counts():
    assert run({"tasks": [], "_meta": {"events": []}}, activity={WORKER: {"watch": 40, "act": 3}}) == [
        {
            "kind": "over monitoring",
            "subject": WORKER,
            "evidence": "40 watch calls against 3 actions",
            "threshold": "at least 20 watch calls and more than 5 per action",
        }
    ]


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
    ],
)
def test_defaults_match_the_toolbelt_rule(name, value):
    assert getattr(LIMITS, name) == value
