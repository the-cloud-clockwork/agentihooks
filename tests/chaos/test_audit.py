from tests.chaos import audit

CLAIM = "c-{}"


def _offer(t, task):
    return {"t": t, "proc": "chaos", "kind": "offered", "task": task}


def _run(t, hive, task, end):
    return [
        {"t": t, "proc": hive, "kind": "launching", "task": task, "claim": CLAIM.format(task)},
        {"t": t + 10, "proc": hive, "kind": "launched", "task": task, "claim": CLAIM.format(task)},
        {"t": end, "proc": hive, "kind": "exited", "task": task, "claim": CLAIM.format(task)},
        {"t": end + 5, "proc": "hub-a", "kind": "settled", "task": task, "claim": CLAIM.format(task)},
    ]


def _clean():
    records = [_offer(0, "t1"), _offer(0, "t2"), _offer(103_500, "t3")]
    records += [{"t": 1, "proc": "chaos", "kind": "lease", "holder": "hub-a"}]
    records += _run(100, "hive-1", "t1", 0)[:2]
    records += [{"t": 150, "proc": "chaos", "kind": "fault", "fault": "hive-kill", "target": "hive-1"}]
    records += _run(60_000, "hive-2", "t1", 66_000)
    records += _run(200, "hive-2", "t2", 6_000)
    records += [{"t": 100_000, "proc": "chaos", "kind": "window-end", "fault": "hive-kill"}]
    records += [{"t": 100_001, "proc": "chaos", "kind": "fault", "fault": "nats-restart", "target": "nats"}]
    records += [{"t": 103_000, "proc": "chaos", "kind": "fault-cleared", "fault": "nats-restart"}]
    records += _run(104_000, "hive-2", "t3", 110_000)
    records += [{"t": 200_000, "proc": "chaos", "kind": "window-end", "fault": "nats-restart"}]
    records += [{"t": 200_001, "proc": "chaos", "kind": "fault", "fault": "hub-kill", "target": "hub-a"}]
    records += [{"t": 200_002, "proc": "chaos", "kind": "lease", "holder": None}]
    records += [{"t": 215_000, "proc": "chaos", "kind": "lease", "holder": "hub-b"}]
    records += [{"t": 215_500, "proc": "hub-b", "kind": "settled", "task": "t3", "claim": "c-late"}]
    records += [{"t": 300_000, "proc": "chaos", "kind": "window-end", "fault": "hub-kill"}]
    return records


def _report(records):
    return audit.report(sorted(records, key=lambda record: record["t"]))


def test_a_clean_run_meets_the_contract_with_every_reclaim_measured():
    found = _report(_clean())
    reclaims = [window["reclaim"] for window in found["windows"]]
    assert found["contract_met"], found
    assert reclaims[0] == {"tasks": {"t1": 59.86}, "max_s": 59.86}
    assert reclaims[1]["first_launch_s"] == 1.01
    assert reclaims[2]["takeover_s"] == 14.999


def test_a_task_running_on_two_live_hives_fails_the_contract():
    found = _report(_clean() + _run(5_000, "hive-3", "t2", 9_000))
    assert found["windows"][0]["duplicate_claims"] == 1
    assert found["final"]["duplicate_claims"] == 1
    assert not found["contract_met"]


def test_a_reaped_leftover_ends_its_run_before_the_relaunch():
    leftover = _run(70_000, "hive-2", "t9", 0)[:2]
    leftover += [{"t": 70_500, "proc": "hive-2", "kind": "reaped", "task": "t9", "claim": CLAIM.format("t9")}]
    found = _report(_clean() + leftover + _run(71_000, "hive-2", "t9", 75_000))
    assert found["final"]["duplicate_claims"] == 0
    assert found["contract_met"], found


def test_work_lost_with_its_hive_is_an_orphan():
    records = [record for record in _clean() if not (record["proc"] == "hive-2" and record.get("task") == "t1")]
    found = _report(records)
    assert found["final"]["orphaned"] == {"t1": "lost with its hive"}
    assert found["windows"][0]["reclaim"]["max_s"] is None
    assert not found["contract_met"]


def test_an_expired_lease_with_no_holder_is_no_takeover():
    records = [record for record in _clean() if record.get("holder") != "hub-b"]
    found = _report(records)
    assert found["windows"][2]["reclaim"]["takeover_s"] is None
    assert not found["contract_met"]


def test_an_exit_the_hub_never_handled_fails_and_a_redelivered_one_does_not():
    unhandled = [record for record in _clean() if not (record["kind"] == "settled" and record["task"] == "t2")]
    assert _report(unhandled)["final"]["exit_events_never_handled"] == 1
    assert not _report(unhandled)["contract_met"]
    twice = _clean() + [{"t": 7_000, "proc": "hub-a", "kind": "settled", "task": "t2", "claim": "c-t2"}]
    assert _report(twice)["final"]["exit_events_handled_twice"] == 1
    assert _report(twice)["contract_met"]
