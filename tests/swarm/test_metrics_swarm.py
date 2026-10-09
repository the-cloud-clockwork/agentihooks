import json
from dataclasses import asdict
from types import SimpleNamespace

import fakeredis
import pytest

from scripts.swarm import metrics_swarm
from scripts.swarm.host_budget import HostSample
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit
SLUG = "scratch"
NOW = 1_800_000_000_000
TASK = {
    "id": "t",
    "state": "pr",
    "lane": "eng",
    "phase": "p",
    "plan_url": "plan",
    "plan_slice": "slice",
    "pr_url": "https://github.com/org/repo/pull/1",
}
AGENT = asdict(
    AgentRecord(
        "worker",
        "eng",
        "t",
        harness="codex",
        account="a",
        model="m",
        effort="high",
        started_at=NOW,
        execution_id="life",
    )
)


def doc(events=()):
    return {"tasks": [TASK], "_meta": {"events": list(events)}}


def event(kind, at, by="worker"):
    return {"kind": kind, "target": "tasks/t", "at": at, "by": by}


def assert_node(row):
    assert {key: row[key] for key in ("ledger", "plan", "phase", "slice", "task")} == {
        "ledger": SLUG,
        "plan": "plan",
        "phase": "p",
        "slice": "slice",
        "task": "t",
    }


def test_full_task_lifecycle_keeps_identity_and_actual_merge_time():
    events = [event("task claimed", NOW), event("task pr", NOW + 1_000)]
    agent = {**AGENT, "ended_at": NOW + 5_000, "reason": "finished"}
    agents = metrics_swarm.agent_rows(SLUG, doc(events), [agent])
    pulls = {TASK["pr_url"]: SimpleNamespace(state="MERGED", merged_at=NOW + 4_000)}
    delivery = metrics_swarm.delivery_rows(SLUG, doc(events), [agent], pulls)
    assert [row["kind"] for row in agents] == ["spawn", "retire", "claim"]
    assert [row["kind"] for row in delivery] == ["pull_request_opened", "merge"]
    assert delivery[-1]["claim_to_merge_seconds"] == 4.0
    assert delivery[-1]["ts_ms"] == NOW + 4_000
    for row in [*agents, *delivery]:
        assert_node(row)
        assert {
            key: row[key] for key in ("agent", "lane", "harness", "account", "model", "effort", "execution_id")
        } == {
            "agent": "worker",
            "lane": "eng",
            "harness": "codex",
            "account": "a",
            "model": "m",
            "effort": "high",
            "execution_id": "life",
        }
    assert len({row["event_id"] for row in [*agents, *delivery]}) == 5
    assert metrics_swarm.agent_rows(SLUG, doc(events), [agent]) == agents


def test_reclaimed_task_attributes_each_claim_to_its_own_life():
    first = {**AGENT, "ended_at": NOW + 500, "reason": "retired"}
    second = {**AGENT, "name": "replacement", "started_at": NOW + 1_000, "execution_id": "other"}
    events = [event("task claimed", NOW), event("task claimed", NOW + 1_000, "replacement")]
    rows = metrics_swarm.agent_rows(SLUG, doc(events), [first, second])
    assert [(row["agent"], row["execution_id"]) for row in rows if row["kind"] == "claim"] == [
        ("worker", "life"),
        ("replacement", "other"),
    ]


def test_gate_denies_idle_and_reruns_keep_observed_time():
    gates = [
        {"at": NOW + 1, "gate": "build", "kind": "deny", "agent": "worker", "task": "t", "reason": "outside scope"},
        {"at": NOW + 2, "gate": "idle-ticks", "kind": "count", "agent": "worker", "task": "t", "reason": "idle tick 1"},
        {
            "at": NOW + 3,
            "gate": "reruns",
            "kind": "count",
            "agent": "worker",
            "task": "t",
            "reason": "CI reruns 1 of 2",
        },
        {"at": NOW + 4, "gate": "build", "kind": "observe", "agent": "worker", "task": "t", "reason": "lifted"},
    ]
    agent, delivery = metrics_swarm.gate_rows(SLUG, doc(), [AGENT], gates)
    assert [(row["kind"], row["ts_ms"], row["reason"]) for row in agent] == [
        ("gate_deny", NOW + 1, "outside scope"),
        ("idle", NOW + 2, "idle tick 1"),
    ]
    assert [(row["kind"], row["ts_ms"]) for row in delivery] == [("rerun", NOW + 3)]


def test_handoff_and_health_finding_have_stable_source_ids():
    handoffs = [{"id": "transfer", "at": NOW + 1, "predecessor": "worker", "task": "t", "reason": "recycle"}]
    found = [
        {
            "id": "idle-with-claim/worker",
            "subject": "worker",
            "kind": "idle with claim",
            "seen_at": NOW + 2,
            "summary": "idle",
        }
    ]
    rows = metrics_swarm.signal_rows(SLUG, doc(), [AGENT], handoffs, found)
    assert [row["kind"] for row in rows] == ["handoff", "health_finding"]
    assert [row["reason"] for row in rows] == ["recycle", "idle"]
    assert (
        metrics_swarm.signal_rows(SLUG, doc(), [AGENT], handoffs, [{**found[0], "summary": "still idle"}])[1][
            "event_id"
        ]
        == rows[1]["event_id"]
    )


def test_host_sample_and_quota_unknown_values_are_explicit():
    capacity = {
        "configured": {"eng": 3},
        "effective": {"eng": 1},
        "reason": "quota closed",
        "accounts": [
            {"harness": "codex", "name": "a", "state": "UNKNOWN", "five_left": None, "week_left": 30.0, "sessions": 1}
        ],
    }
    capacity["held_spawns"] = 2
    host = metrics_swarm.host_row(SLUG, NOW, HostSample(4.0, 2, 1024, 3), capacity)
    assert {key: host[key] for key in ("available_mb", "load_per_cpu", "live_agents", "held_spawns", "reason")} == {
        "available_mb": 1024,
        "load_per_cpu": 2.0,
        "live_agents": 3,
        "held_spawns": 2,
        "reason": "quota closed",
    }
    rows = metrics_swarm.quota_rows(SLUG, NOW, capacity)
    assert {
        key: rows[0][key]
        for key in ("harness", "account", "state", "five_left", "five_known", "week_left", "week_known", "sessions")
    } == {
        "harness": "codex",
        "account": "a",
        "state": "UNKNOWN",
        "five_left": -1.0,
        "five_known": 0,
        "week_left": 30.0,
        "week_known": 1,
        "sessions": 1,
    }


def test_classifier_calls_are_host_scoped_with_stable_source_ids():
    call = {
        "ts": "2027-01-15T08:00:00+00:00",
        "purpose": "intent",
        "source": "api",
        "latency_ms": 15,
        "answers": {"ready": {"value": True}},
        "state_digest": "digest",
    }
    rows = metrics_swarm.classifier_rows([call, call], "machine")
    assert len(rows) == 2
    assert rows[0]["event_id"] != rows[1]["event_id"]
    assert {key: rows[0][key] for key in ("ledger", "plan", "phase", "slice", "task", "host")} == {
        "ledger": "host:machine",
        "plan": "",
        "phase": "",
        "slice": "",
        "task": "",
        "host": "machine",
    }
    assert {key: rows[0][key] for key in ("definition", "backend", "latency_ms", "verdict")} == {
        "definition": "intent",
        "backend": "api",
        "latency_ms": 15,
        "verdict": '{"ready": {"value": true}}',
    }
    assert metrics_swarm.classifier_rows([call, call], "machine") == rows


def test_unknown_claim_duration_is_not_invented():
    pulls = {TASK["pr_url"]: SimpleNamespace(state="MERGED", merged_at=NOW + 4_000)}
    row = metrics_swarm.delivery_rows(SLUG, doc(), [AGENT], pulls)[0]
    assert row["claim_to_merge_seconds"] == -1.0


def test_record_pass_appends_through_outbox_and_deduplicates(tmp_path, monkeypatch):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 0, 0))
    store.put_agent(SLUG, AgentRecord(**{key: value for key, value in AGENT.items()}))
    state = doc([event("task claimed", NOW)])
    monkeypatch.setattr(metrics_swarm.host_budget, "read_host", lambda: HostSample(2.0, 2, 512, 1))
    monkeypatch.setattr(metrics_swarm.gate_log, "recent", lambda *args, **kwargs: [])
    monkeypatch.setattr(metrics_swarm, "read_classifier_calls", lambda: [])
    monkeypatch.setattr(metrics_swarm, "read_review_events", lambda slug: [])
    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "", ""))
    try:
        metrics_swarm.record_pass(box, SLUG, NOW, store, state, [], {})
        metrics_swarm.record_pass(box, SLUG, NOW, store, state, [], {})
        assert len(box.recent("agent_events", NOW)) == 2
        assert len(box.recent("host_samples", NOW)) == 1
        for row in box.recent("agent_events", NOW):
            assert_node(row)
        store.drop_agent(SLUG, "worker", NOW + 5_000)
        metrics_swarm.record_pass(box, SLUG, NOW + 6_000, store, state, [], {})
        assert [row["kind"] for row in box.recent("agent_events", NOW + 6_000)] == ["spawn", "claim", "retire"]
    finally:
        box.close()


def test_review_round_uses_stored_review_event():
    state = doc([event("review round", NOW + 2_000)])
    rows = metrics_swarm.delivery_rows(SLUG, state, [AGENT], {})
    assert [(row["kind"], row["ts_ms"]) for row in rows] == [("review_round", NOW + 2_000)]


def test_reviews_reuse_the_existing_intent_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_swarm.gate_log, "gates_dir", lambda slug: tmp_path)
    assert metrics_swarm.read_review_events(SLUG) == []
    folder = tmp_path / "intent"
    folder.mkdir()
    state = {
        "reviewer_findings": {
            "reviews": [
                {"id": "review", "submittedAt": "2027-01-15T08:00:00+00:00"},
                {"id": "pending", "submittedAt": None},
            ]
        }
    }
    source = {"task": "t", "agent": "worker", "classifier_input": json.dumps({"state": state})}
    (folder / "history.jsonl").write_text(json.dumps(source) + "\n")
    events = metrics_swarm.read_review_events(SLUG)
    assert events == [{"kind": "review round", "target": "tasks/t", "at": NOW, "by": "worker", "review_id": "review"}]
    rows = metrics_swarm.delivery_rows(SLUG, doc(events), [AGENT], {})
    assert len(rows) == 1
    assert rows[0]["kind"] == "review_round"


def test_held_spawns_count_ready_tasks_with_room_but_without_placements(monkeypatch):
    ready = {lane: [] for lane in metrics_swarm.capacity.LANES}
    ready["eng"] = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    monkeypatch.setattr(metrics_swarm.capacity, "ready_work", lambda *args: ({}, ready))
    quota = {"configured": {"eng": 3}, "placements": {"eng": [{"index": 0}]}}
    assert metrics_swarm._held_spawns(None, SLUG, doc(), quota, [AGENT]) == 1
    assert metrics_swarm._held_spawns(None, SLUG, doc(), quota, []) == 2
    assert metrics_swarm._held_spawns(None, SLUG, doc(), {}, [AGENT]) == 0


def test_all_generated_rows_match_the_shared_outbox_schema():
    state = doc([event("task claimed", NOW), event("task pr", NOW + 1)])
    pulls = {TASK["pr_url"]: SimpleNamespace(state="MERGED", merged_at=NOW + 2)}
    for table, rows in (
        (metrics_swarm.AGENTS, metrics_swarm.agent_rows(SLUG, state, [AGENT])),
        (metrics_swarm.DELIVERY, metrics_swarm.delivery_rows(SLUG, state, [AGENT], pulls)),
        (metrics_swarm.HOST, [metrics_swarm.host_row(SLUG, NOW, HostSample(1.0, 1, 1, 1), {})]),
    ):
        for row in rows:
            assert table.check(row) is None
            assert json.loads(json.dumps(row)) == row
