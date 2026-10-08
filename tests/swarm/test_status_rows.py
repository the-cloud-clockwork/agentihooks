import json

import pytest

from scripts.swarm import status
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")
MIDNIGHT = 1_791_244_800_000
HOUR = 3_600_000


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def transfer(seat, at, **fields):
    return {
        "seat": seat,
        "at": at,
        "reason": "recycle",
        "successor": "",
        "handoff": "long text never sent to the page",
        "continuity": {"state": "pending"},
        "binding": {"state": "pending"},
        **fields,
    }


def test_handoff_rows_keep_the_latest_transfer_per_seat_with_the_master_first():
    rows = status.handoff_rows(
        [
            transfer("eng-1@sw", 10, successor="sw-eng-4", continuity={"state": "confirmed"}),
            transfer("master@sw", 20, successor="sw-master-2", binding={"state": "live", "at": 25}),
            transfer("eng-1@sw", 30, reason="crash", successor="sw-eng-5"),
        ],
        [{"name": "sw-eng-5", "seat": "eng-1@sw", "state": "working"}],
    )
    assert rows == [
        {
            "seat": "master@sw",
            "at": 20,
            "reason": "recycle",
            "continuity": "pending",
            "binding": "live",
            "bound_at": 25,
            "successor": "sw-master-2",
            "awaiting": "",
        },
        {
            "seat": "eng-1@sw",
            "at": 30,
            "reason": "crash",
            "continuity": "pending",
            "binding": "pending",
            "bound_at": 0,
            "successor": "sw-eng-5",
            "awaiting": "",
        },
    ]


def test_a_seat_without_a_handoff_shows_its_live_occupant():
    rows = status.handoff_rows([], [{"name": "sw-eng-9", "seat": "eng-2@sw", "state": "working"}])
    assert rows == [
        {
            "seat": "eng-2@sw",
            "at": 0,
            "reason": "",
            "continuity": "",
            "binding": "live",
            "bound_at": 0,
            "successor": "sw-eng-9",
            "awaiting": "",
        }
    ]


def test_a_seat_whose_agent_awaits_a_restore_decision_names_it():
    rows = status.handoff_rows([], [{"name": "sw-eng-3", "seat": "eng-3@sw", "state": "awaiting-decision"}])
    assert rows[0]["awaiting"] == "sw-eng-3"
    assert rows[0]["binding"] == "awaiting decision"


def test_seats_after_the_master_come_in_name_order():
    agents = [
        {"name": n, "seat": s, "state": "working"} for n, s in (("c", "plan-1@sw"), ("a", "ci-1@sw"), ("b", "eng-1@sw"))
    ]
    rows = status.handoff_rows([transfer("master@sw", 1)], agents)
    assert [r["seat"] for r in rows] == ["master@sw", "ci-1@sw", "eng-1@sw", "plan-1@sw"]


def test_an_agent_without_a_seat_adds_no_row():
    assert status.handoff_rows([], [{"name": "x", "seat": "", "state": "working"}]) == []


def test_done_today_counts_tasks_marked_done_since_local_midnight_once_each():
    events = [
        {"kind": "task done", "target": "tasks/a", "at": MIDNIGHT - 1},
        {"kind": "task done", "target": "tasks/b", "at": MIDNIGHT + HOUR},
        {"kind": "task done", "target": "tasks/b", "at": MIDNIGHT + 2 * HOUR},
        {"kind": "task done", "target": "tasks/c", "at": MIDNIGHT + HOUR},
        {"kind": "task pr", "target": "tasks/d", "at": MIDNIGHT + HOUR},
    ]
    tasks = [{"id": i, "state": "done"} for i in "abd"] + [{"id": "c", "state": "open"}]
    assert status.done_today(tasks, events, MIDNIGHT) == 1


def test_done_today_needs_a_done_task_and_a_done_event_from_midnight_on():
    events = [
        {"kind": "task done", "target": "tasks/a", "at": MIDNIGHT - 1},
        {"kind": "task done", "target": "tasks/b", "at": MIDNIGHT},
        {"kind": "task done", "target": "tasks/c", "at": MIDNIGHT + HOUR},
        {"kind": "task done", "target": "tasks/e", "at": MIDNIGHT + HOUR},
        {"kind": "task pr", "target": "tasks/d", "at": MIDNIGHT + HOUR},
    ]
    tasks = [{"id": i, "state": "done"} for i in "abde"] + [{"id": "c", "state": "open"}]
    assert status.done_today(tasks, events, MIDNIGHT) == 2


def test_local_midnight_is_the_start_of_today_in_milliseconds():
    import time

    assert status.local_midnight_ms() == int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) * 1000)


def test_seat_rows_default_to_empty_text_without_a_name_or_binding():
    rows = status.handoff_rows([{"seat": "eng-1@sw", "at": 1}], [{"seat": "eng-2@sw", "state": "awaiting-decision"}])
    assert [(r["seat"], r["binding"], r["successor"], r["awaiting"]) for r in rows] == [
        ("eng-1@sw", "", "", ""),
        ("eng-2@sw", "live", "", ""),
    ]


def test_doctor_is_not_running_without_a_peer(store):
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    assert status.doctor_report(store, "sw") == {"slug": "", "state": "not running", "last_check": 0, "findings": 0}


def test_doctor_reports_its_state_last_pass_and_findings(store):
    from scripts.doctor import loop

    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.create(SwarmConfig("sw-doctor", "/repo", 1, 0, state="paused"))
    store.set_peer("sw", "sw-doctor")
    store.redis.hset(store.key("sw-doctor", "doctor-timer"), mapping={"last": 42, "last_new": 40})
    store.redis.hset(loop.verdicts(store, "sw-doctor").key, mapping={"a/x": "{}", "b/y": "{}"})
    assert status.doctor_report(store, "sw") == {
        "slug": "sw-doctor",
        "state": "paused",
        "last_check": 42,
        "findings": 2,
    }


def test_a_doctor_before_its_first_pass_has_no_last_check(store):
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.create(SwarmConfig("sw-doctor", "/repo", 1, 0))
    store.set_peer("sw", "sw-doctor")
    assert status.doctor_report(store, "sw")["last_check"] == 0


def test_doctor_whose_swarm_is_gone_reads_not_running(store):
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.set_peer("sw", "sw-doctor")
    assert status.doctor_report(store, "sw")["state"] == "not running"


def test_compact_limit_is_the_swarm_setting_else_the_environment_else_six_hundred(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_COMPACT_LIMIT", raising=False)
    assert status.compact_limit(SwarmConfig("sw", "/r", 1, 1)) == 600
    monkeypatch.setenv("AGENTIHOOKS_COMPACT_LIMIT", "450")
    assert status.compact_limit(SwarmConfig("sw", "/r", 1, 1)) == 450
    assert status.compact_limit(SwarmConfig("sw", "/r", 1, 1, compact_limit=300)) == 300


def test_status_report_carries_the_page_rows_without_handoff_text(store, monkeypatch):
    store.create(SwarmConfig("sw", "/repo", 1, 1, compact_limit=250))
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw"))
    store.redis.hset(store.key("sw", "transfers"), "r1", json.dumps({**transfer("eng-1@sw", 5), "id": "r1"}))
    monkeypatch.setattr(status, "page_quota", lambda: {"cap": 3, "rows": [{"account": "a"}]})
    now = status.local_midnight_ms() + 1
    state = {
        "tasks": [{"id": "t1", "state": "done"}],
        "_meta": {"events": [{"kind": "task done", "target": "tasks/t1", "at": now, "by": "sw-eng-1"}]},
    }
    report = status.status_report(store, "sw", state)
    assert report["compact_limit"] == 250
    assert report["transfers"][0]["seat"] == "eng-1@sw"
    assert report["done_today"] == 1
    assert report["quota"] == {"cap": 3, "rows": [{"account": "a"}]}
    assert report["doctor"]["state"] == "not running"
    assert report["handoffs"][0]["seat"] == "eng-1@sw"
    assert "long text" not in json.dumps(report["handoffs"])


def test_status_raises_a_base_miss_as_a_finding(store, monkeypatch):
    from scripts.swarm import launch_check

    store.create(SwarmConfig("sw", "/repo", 1, 1))
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1")
    found = {"base": {"expected": "package:engineer", "actual": "engineer"}}
    saved = launch_check.record(store, "sw", agent, found, 10, 60_000)
    monkeypatch.setattr(status, "page_quota", lambda: {})
    report = status.status_report(store, "sw", {"tasks": []})
    assert report["launch_checks"] == [saved]
    assert [f["id"] for f in report["findings"]] == ["launch-check/engineer@a1b2c3-0001/base"]
