import copy
import json

import pytest

from scripts.doctor import spawns
from tests.doctor.recorded import load
from tests.swarm.profile_fixture import validated


def test_recorded_spawns_and_planted_failed_spawn():
    record = load("spawns")
    assert spawns.failed(record) == []
    planted = copy.deepcopy(record)
    planted["actions"].append(f"{record['slug']}: spawn failed for dt2: init-agent timed out")
    [found] = spawns.failed(planted)
    assert found.id == "failed-spawn/dt2"
    assert found.measure == 1
    assert "init-agent timed out" in found.evidence[0]
    assert record["actions"] == []


def test_reservation_refusals_require_recorded_free_sessions():
    record = load("spawns")
    slug = record["slug"]
    refusal = f"{slug}: spawn failed for dt2: account reservation is full"
    accounts = [
        {"harness": "claude", "state": "OPEN", "sessions": 6, "cap": 6},
        {"harness": "codex", "state": "OPEN", "sessions": 0, "cap": 8},
    ]
    capacity = {"harness": "claude", "accounts": accounts}
    record["actions"] = [f"{slug}: spawn capacity for dt2: {json.dumps(capacity)}", refusal]
    assert spawns.failed(record) == []
    accounts[0]["sessions"] = 5
    record["actions"] = [f"{slug}: spawn capacity for dt2: {json.dumps(capacity)}", refusal]
    [found] = spawns.failed(record)
    assert found.id == "failed-spawn/dt2"
    assert found.measure == 1
    assert found.evidence == ("account reservation is full",)


@pytest.mark.parametrize(
    ("harness", "state", "sessions", "cap", "count"),
    [
        ("claude", "OPEN", 5, 6, 1),
        ("claude", "OPEN", 6, 6, 0),
        ("claude", "OPEN", 7, 6, 0),
        ("codex", "OPEN", 0, 6, 0),
        ("claude", "DRAIN", 0, 6, 0),
        ("claude", "UNKNOWN", 0, 6, 0),
        ("claude", "OPEN", 0, None, 0),
        ("claude", "OPEN", 0, 0, 0),
    ],
)
def test_only_eligible_accounts_for_the_refused_harness_count(harness, state, sessions, cap, count):
    record = load("spawns")
    slug = record["slug"]
    snapshot = {
        "harness": "claude",
        "accounts": [{"harness": harness, "state": state, "sessions": sessions, "cap": cap}],
    }
    record["actions"] = [
        f"{slug}: spawn capacity for dt2: {json.dumps(snapshot)}",
        f"{slug}: spawn failed for dt2: account reservation is full",
    ]
    found = spawns.failed(record)
    assert len(found) == count
    if count:
        assert found[0].measure == 1


def test_capacity_evidence_is_consumed_by_one_failure_and_other_errors_remain():
    record = load("spawns")
    slug = record["slug"]
    snapshot = {
        "harness": "codex",
        "accounts": [{"harness": "codex", "state": "OPEN", "sessions": 1, "cap": 2}],
    }
    record["actions"] = [
        f"other: spawn capacity for dt2: {json.dumps(snapshot)}",
        f"{slug}: spawn failed for dt2: account reservation is full",
        f"{slug}: spawn capacity for dt2: {json.dumps(snapshot)}",
        f"{slug}: spawn failed for dt2: account reservation is full",
        f"{slug}: spawn failed for dt2: account reservation is full",
        f"{slug}: spawn failed for dt2: init-agent timed out",
        f"{slug}: master spawn failed: boom",
    ]
    found = {finding.subject: finding for finding in spawns.failed(record)}
    assert found["dt2"].measure == 2
    assert found["dt2"].evidence == ("account reservation is full", "init-agent timed out")
    assert found["master"].evidence == ("boom",)


@pytest.mark.parametrize("kind", ["hive", "headless hive", "UI hive"])
@pytest.mark.parametrize(
    "limits",
    [
        ["a cpu: five minute load 8.00 on 10 CPUs, 0.5 CPU per agent, fits 0 more under the 80 percent line"],
        ["a ram: 20000 of 100000 MB available, 700 MB per agent, fits 0 more under the 80 percent line"],
        [
            "a cpu: five minute load 8.00 on 10 CPUs, 0.5 CPU per agent, fits 0 more under the 80 percent line",
            "b ram: 20700 of 100000 MB available, 700 MB per agent with 1 pending launches, "
            "fits 0 more under the 80 percent line",
        ],
    ],
)
@pytest.mark.parametrize("local", ["", ", and the local machine runs 8 of its 8 agents"])
def test_host_room_refusals_are_not_failed_spawns(kind, limits, local):
    record = load("spawns")
    record["actions"] = [
        f"{record['slug']}: spawn failed for dt2: no {kind} for eng has room: {'; '.join(limits)}{local}"
    ]
    assert spawns.failed(record) == []


@pytest.mark.parametrize(
    "reason",
    [
        "init-agent timed out on a hive with room",
        "hive launch failed: a cpu: process exited with code 1",
        "no hive for eng has room: a missing telemetry: hive reading unavailable",
        "no hive for eng has room: a max agents: 3 of 3 agents on the hive, fits 0 more",
        "no hive for eng has room: a cpu: fits 0 more under the 80 percent line; "
        "b missing telemetry: hive reading unavailable",
    ],
)
def test_host_room_filter_keeps_other_launch_faults(reason):
    record = load("spawns")
    record["actions"] = [
        f"{record['slug']}: spawn failed for dt2: no hive for eng has room: "
        "a ram: 20000 of 100000 MB available, 700 MB per agent, fits 0 more under the 80 percent line",
        f"{record['slug']}: spawn failed for dt2: {reason}",
    ]
    [found] = spawns.failed(record)
    assert found.id == "failed-spawn/dt2"
    assert found.measure == 1
    assert found.evidence == (reason,)


def test_reopened_suffix_keeps_the_subject_and_the_whole_reason():
    record = load("spawns")
    record["actions"] = [
        f"{record['slug']}: spawn failed for dt2, task dt2 reopened: hive launch failed: a cpu: exited 1",
        f"{record['slug']}: spawn failed for dt2, task dt2 reopened: no hive for eng has room: "
        "a ram: 20000 of 100000 MB available, 700 MB per agent, fits 0 more under the 80 percent line",
    ]
    [found] = spawns.failed(record)
    assert found.id == "failed-spawn/dt2"
    assert found.evidence == ("hive launch failed: a cpu: exited 1",)


def test_runtime_records_the_launcher_overflow_placement(tmp_path):
    import os
    import subprocess

    import fakeredis

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import tick

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, state="running")
    store.create(config)
    launched = []

    def run(argv, **kwargs):
        launched.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            validated(
                argv,
                "status=started\nroute_status=routed\nagent=codex\naccount=default\nplacement=overflow\npane_id=1\n",
            ).replace('"pid": 123', f'"pid": {os.getpid()}'),
            "",
        )

    runtime = HerdrRuntime(
        home=tmp_path, run=run, choose=lambda *args: ("codex", "fallthrough: claude is at its session cap")
    )
    runtime.live_names = lambda: set()
    runtime.conversations = lambda: {}
    runtime.has_capacity = lambda config: True
    runtime.quota_capacity = None

    class Ledger:
        def state(self, slug):
            return {"tasks": self.tasks(slug)}

        def tasks(self, slug):
            return [
                {
                    "id": "task",
                    "title": "Task",
                    "description": "",
                    "lane": "eng",
                    "profile": "engineer",
                    "state": "open",
                    "depends_on": [],
                    "territory": [],
                }
            ]

        def update_task(self, slug, task_id, fields, **kwargs):
            return {"state": "open", "claimed_by": "", **fields}

        def notify(self, *args):
            pass

        def comment(self, *args):
            pass

    actions = tick("sw", store, Ledger(), runtime, 1000)
    agents = store.agents("sw")
    assert len(agents) == 2, actions
    assert all(a.placement == "overflow" for a in agents)
    assert [a.choice for a in agents if a.lane == "eng"] == ["overflow"]


def test_recorded_restores_and_planted_fresh_fallback():
    record = load("spawns")
    assert spawns.fresh_restores(record) == []
    planted = copy.deepcopy(record)
    planted["restored"] = [
        {
            "name": "agent",
            "task": "dt2",
            "lane": "eng",
            "outcome": "fresh",
            "reason": "conversation is unavailable",
            "conversation_id": "prior",
            "at": 1000,
        }
    ]
    [found] = spawns.fresh_restores(planted)
    assert found.id == "fresh-restore/agent"
    assert found.measure == 1
    assert "conversation is unavailable" in found.evidence
    planted["restored"][0]["outcome"] = "resumed"
    assert spawns.fresh_restores(planted) == []


def test_recorded_agents_with_no_hook_event_one_tick_after_spawn_are_flagged():
    agents = load("spawns")["agents"]
    last = max(a["started_at"] for a in agents)
    found = spawns.silent_starts(agents, {}, last + spawns.TICK_MS)
    assert [f.id for f in found] == sorted(f"no-hook-event/{a['name']}" for a in agents)
    assert all(f.measure >= 1 for f in found)
    assert spawns.silent_starts(agents, {a["name"]: a["started_at"] + 1 for a in agents}, last + spawns.TICK_MS) == []
    assert spawns.silent_starts(agents, {}, last + spawns.TICK_MS - 1) == [
        f for f in found if f.subject != max(agents, key=lambda a: a["started_at"])["name"]
    ]
    finished = [{**a, "state": "finished"} for a in agents]
    assert spawns.silent_starts(finished, {}, last + spawns.TICK_MS) == []


ACKED = 50_000


def _hive(name, **fields):
    return {
        "name": name,
        "state": "working",
        "started_at": 1_000,
        "harness": "claude",
        "pane_id": "w4:p1",
        "hive": "h1",
        "launched_at": ACKED - 5_000,
        "launch_timings": {"returned_at": ACKED},
        **fields,
    }


def test_a_running_hive_agent_is_judged_from_its_heartbeat():
    agent = _hive("engineer@a-1")
    later = ACKED + 10 * spawns.TICK_MS
    assert spawns.silent_starts([agent], {}, later, beats={agent["name"]: ACKED + 1}) == []
    assert spawns.silent_starts([agent], {}, later, beats={agent["name"]: 1_000}) == []
    [stale] = spawns.silent_starts([agent], {}, later, beats={agent["name"]: 999})
    assert stale.measure == 10


def test_a_hive_agent_that_never_starts_is_raised_one_tick_after_its_acknowledgement():
    agent = _hive("engineer@a-2")
    assert spawns.silent_starts([agent], {}, ACKED + spawns.TICK_MS - 1) == []
    [found] = spawns.silent_starts([agent], {}, ACKED + 2 * spawns.TICK_MS + 1, beats={"engineer@a-1": ACKED})
    assert found.id == "no-hook-event/engineer@a-2"
    assert found.measure == 2
    assert found.evidence == (f"acknowledged at {ACKED}", "hive h1", "harness claude", "pane w4:p1")
    assert found.threshold == "no heartbeat one tick after the hive acknowledgement"
    assert found.summary == "hive agent has sent no heartbeat since its hive acknowledged the launch"
    unreturned = {k: v for k, v in _hive("engineer@a-2", launch_timings={}).items() if k not in ("harness", "pane_id")}
    [found] = spawns.silent_starts([unreturned], {}, ACKED - 5_000 + spawns.TICK_MS)
    assert found.evidence == (f"acknowledged at {ACKED - 5_000}", "hive h1", "harness ", "pane ")


def test_a_launch_still_waiting_for_its_hive_acknowledgement_is_not_raised():
    starting = {"name": "engineer@a-3", "state": "starting", "started_at": 1_000}
    later = 1_000 + 5 * spawns.TICK_MS
    assert spawns.silent_starts([starting], {}, later, pending={"engineer@a-3"}) == []
    assert [f.subject for f in spawns.silent_starts([starting], {}, later, pending={"other"})] == ["engineer@a-3"]


def test_a_local_agent_keeps_the_first_event_rule():
    agent = {
        "name": "engineer@a-4",
        "state": "working",
        "started_at": 1_000,
        "launched_at": 3_000,
        "launch_timings": {"returned_at": 4_000},
    }
    [found] = spawns.silent_starts([agent], {}, 1_000 + spawns.TICK_MS, beats={agent["name"]: 2_000})
    assert found.measure == 1
    assert found.evidence == ("started at 1000", "harness ", "pane ")
    assert found.threshold == "no hook event one tick after spawn"
    assert spawns.silent_starts([agent], {agent["name"]: 2_000}, 1_000 + spawns.TICK_MS, beats={}) == []
    late = {**agent, "started_at": 1_000_000}
    [found] = spawns.silent_starts([late], {}, 1_000_000 + 2 * spawns.TICK_MS)
    assert found.measure == 2
