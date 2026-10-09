import json
from types import SimpleNamespace

import pytest

from scripts.swarm import metrics_outbox, metrics_swarm
from scripts.swarm.host_budget import HostSample
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_metrics_swarm import AGENT, NOW, SLUG, TASK, assert_node, doc, event

pytestmark = pytest.mark.unit


@pytest.fixture
def box(tmp_path):
    spool = Outbox(tmp_path / "outbox.db", Settings("http://sink", "", ""))
    yield spool
    spool.close()


def test_persisted_identifiers_remain_compatible_for_recorded_inputs():
    state = doc([event("task claimed", NOW), event("task pr", NOW + 1_000)])
    agent = {**AGENT, "ended_at": NOW + 5_000, "reason": "finished"}
    agents = {row["kind"]: row["event_id"] for row in metrics_swarm.agent_rows(SLUG, state, [agent])}
    assert agents == {
        "spawn": "beff12128800576c5dc9ef36f2f3344c19808b6323db93e5455eb8b3e08c3e0b",
        "retire": "312d366bb81006aafe4b57de75c8450fc64ff9f707ca57a9b6f97d148896a796",
        "claim": "7b08d6ace10bbb278e04057e8e7ce80be4a5a0495247e01e22660f498c4cffc1",
    }
    pulls = {TASK["pr_url"]: SimpleNamespace(state="MERGED", merged_at=NOW + 4_000)}
    assert {row["kind"]: row["event_id"] for row in metrics_swarm.delivery_rows(SLUG, state, [agent], pulls)} == {
        "pull_request_opened": "868d759174d90f18b90b12c6491d231e14fb2813444e5350a06f9e2376687bf7",
        "merge": "c405b9e5687a19a1ec4d5ca9431bde100ce7c32916813e2c8ee6424aeadb7793",
    }
    host = metrics_swarm.host_row(SLUG, NOW, HostSample(1.0, 1, 1, 1), {})
    assert host["event_id"] == "3277834f4d5e7cf2b8fb56e7c674ad57cece7f319bbdff0436b21ae8c3758844"
    quota = {
        "accounts": [
            {"harness": "codex", "name": "a", "state": "OPEN", "five_left": 0, "week_left": None, "sessions": 1}
        ]
    }
    assert (
        metrics_swarm.quota_rows(SLUG, NOW, quota)[0]["event_id"]
        == "f27458af1d0a3ecdd41de657d9c7dd7086b1eea6e09fa2fe15237774f2dff8f6"
    )
    call = {
        "_source_id": "record",
        "ts": "2027-01-15T08:00:00+00:00",
        "purpose": "intent",
        "source": None,
        "latency_ms": 1,
        "answers": {},
    }
    assert (
        metrics_swarm.classifier_rows([call], "machine")[0]["event_id"]
        == "8d94cb92b46bd062190991419e3b60d33b6500bf2a7fb9e5a006a4005ff125ed"
    )
    handoffs = [{"id": "transfer", "at": NOW + 1, "task": "t", "predecessor": "worker", "reason": "recycle"}]
    found = [
        {
            "id": "idle-with-claim/worker",
            "kind": "idle with claim",
            "seen_at": NOW + 2,
            "subject": "worker",
            "summary": "idle",
        }
    ]
    assert {
        row["kind"]: row["event_id"] for row in metrics_swarm.signal_rows(SLUG, state, [agent], handoffs, found)
    } == {
        "handoff": "cc25e6455f9b62a5b5c9955196d6b7f781eb0a0c79f74c99f657bc3098cd7267",
        "health_finding": "9ba749982b5834cb65e8d4b5c8ccfb54d1d6de2b1384df02beb27b3c59649127",
    }


def test_event_replay_ignores_mapping_order_and_keeps_separate_claims():
    first = event("task claimed", NOW, "controller")
    second = event("task claimed", NOW + 1, "controller")
    rows = metrics_swarm.agent_rows(SLUG, doc([first, second]), [AGENT])
    reordered = [{key: item[key] for key in reversed(item)} for item in [first, second]]
    assert metrics_swarm.agent_rows(SLUG, doc(reordered), [AGENT]) == rows
    assert len({row["event_id"] for row in rows}) == 3
    assert [row["reason"] for row in rows] == ["", "", ""]
    assert metrics_swarm.agent_rows(SLUG, {}, []) == []
    assert metrics_swarm.agent_rows(SLUG, {"_meta": {}}, []) == []
    mixed = doc([event("comment", NOW), *[first, second]])
    assert metrics_swarm.agent_rows(SLUG, mixed, [AGENT]) == rows


def test_temporal_ownership_excludes_other_tasks_and_ended_lives():
    old = {**AGENT, "ended_at": NOW + 1, "reason": "retired"}
    new = {**AGENT, "name": "replacement", "started_at": NOW + 2, "execution_id": "new"}
    unrelated = {**AGENT, "name": "XXXX", "task": "other", "started_at": NOW + 3}
    state = doc([event("task claimed", NOW + 4, "controller")])
    rows = metrics_swarm.agent_rows(SLUG, state, [old, new, unrelated])
    claim = next(row for row in rows if row["kind"] == "claim")
    assert claim["agent"] == "replacement"
    assert claim["execution_id"] == "new"
    overlap = {**old, "ended_at": NOW + 10}
    claim = metrics_swarm.agent_rows(SLUG, state, [overlap, new])[-1]
    assert claim["agent"] == "replacement"
    explicit = doc([event("task claimed", NOW + 4, "worker")])
    assert metrics_swarm.agent_rows(SLUG, explicit, [old, new])[-1]["agent"] == "worker"
    unknown = metrics_swarm.agent_rows(SLUG, state, [])[-1]
    assert_node(unknown)
    assert {key: unknown[key] for key in metrics_swarm.IDENTITY} == dict.fromkeys(metrics_swarm.IDENTITY, "")
    missing_task = metrics_swarm.agent_rows(SLUG, {}, [unrelated])[0]
    assert missing_task["task"] == "other"
    assert [missing_task[key] for key in ("plan", "phase", "slice")] == ["", "", ""]


def test_nonmerged_pulls_do_not_hide_later_merges_and_duration_starts_at_first_claim():
    other = {**TASK, "id": "other", "pr_url": "other", "state": "pr"}
    state = {
        "tasks": [other, TASK],
        "_meta": {"events": [event("task claimed", NOW), event("task claimed", NOW + 1_000)]},
    }
    pulls = {
        "other": SimpleNamespace(state="OPEN", merged_at=NOW),
        TASK["pr_url"]: SimpleNamespace(state="MERGED", merged_at=NOW + 4_000),
    }
    rows = metrics_swarm.delivery_rows(SLUG, state, [AGENT], pulls)
    assert len(rows) == 1
    assert rows[0]["claim_to_merge_seconds"] == 4.0
    assert_node(rows[0])
    assert metrics_swarm.delivery_rows(SLUG, {}, [], {}) == []
    assert metrics_swarm.delivery_rows(SLUG, {"_meta": {}}, [], {}) == []
    rows = metrics_swarm.delivery_rows(SLUG, doc([event("review round", NOW)]), [], {})
    assert rows[0]["reason"] == ""
    no_url = {
        "tasks": [{key: value for key, value in TASK.items() if key != "pr_url"}],
        "_meta": {"events": [event("task pr", NOW)]},
    }
    assert metrics_swarm.delivery_rows(SLUG, no_url, [], {})[0]["pull_request"] == ""


def test_distinct_review_and_gate_events_keep_complete_node_and_actor_fields():
    agents = [{**AGENT, "ended_at": NOW - 1, "reason": "retired"}, {**AGENT, "name": "replacement", "started_at": NOW}]
    reviews = [
        {**event("review round", NOW + 1), "review_id": "r1"},
        {**event("review round", NOW + 2), "review_id": "r2"},
    ]
    rows = metrics_swarm.delivery_rows(SLUG, doc(reviews), agents, {})
    assert len({row["event_id"] for row in rows}) == 2
    assert [row["agent"] for row in rows] == ["worker", "worker"]
    gates = [
        {"at": NOW + 1, "gate": "build", "kind": "deny", "agent": "worker", "task": "t", "reason": "outside"},
        {"at": NOW + 2, "gate": "idle-ticks", "kind": "count", "agent": "worker", "task": "t", "reason": "idle"},
        {"at": NOW + 3, "gate": "reruns", "kind": "count", "agent": "worker", "task": "t", "reason": "retry"},
        {"at": NOW + 4, "gate": "build", "kind": "count", "agent": "worker", "task": "t", "reason": "other"},
    ]
    rows, delivery = metrics_swarm.gate_rows(SLUG, doc(), agents, gates)
    assert [row["kind"] for row in rows] == ["gate_deny", "idle"]
    assert [row["reason"] for row in delivery] == ["retry"]
    for row in rows + delivery:
        assert_node(row)
        assert row["agent"] == "worker"
    twice, reruns = metrics_swarm.gate_rows(SLUG, doc(), agents, gates + gates)
    assert len({row["event_id"] for row in twice}) == 2
    assert len({row["event_id"] for row in reruns}) == 1


def test_health_and_handoff_scopes_include_agent_task_and_host_findings():
    handoff = {"id": "transfer", "at": NOW, "predecessor": "worker", "task": "t", "reason": "recycle"}
    found = [
        {"id": "agent", "kind": "stale claim", "seen_at": NOW, "subject": "worker", "summary": "agent"},
        {"id": "task", "kind": "proof loop", "seen_at": NOW, "subject": "t", "summary": "task"},
        {"id": "host", "kind": "host pressure", "seen_at": NOW, "subject": "machine", "summary": "host"},
    ]
    rows = metrics_swarm.signal_rows(SLUG, doc(), [AGENT], [handoff], found)
    for row in rows[:3]:
        assert_node(row)
        assert row["agent"] == "worker"
        assert row["ts_ms"] == NOW
    assert rows[-1]["task"] == ""
    assert rows[-1]["ledger"] == SLUG
    assert rows[-1]["agent"] == ""
    assert len({row["event_id"] for row in rows}) == 4
    assert metrics_swarm.signal_rows(SLUG, doc(), [], [], [found[1]])[0]["task"] == "t"
    assert metrics_swarm.signal_rows(SLUG, {}, [], [], [found[-1]])[0]["task"] == ""


def test_quota_and_classifier_unknowns_keep_observed_values_and_canonical_verdicts():
    quota = {
        "accounts": [
            {"harness": "codex", "name": "a", "state": "OPEN", "five_left": 0, "week_left": None, "sessions": 2}
        ]
    }
    row = metrics_swarm.quota_rows(SLUG, NOW, quota)[0]
    assert row["ledger"] == SLUG
    assert row["ts_ms"] == NOW
    assert row["five_left"] == 0.0 and row["five_known"] == 1
    assert row["week_left"] == -1.0 and row["week_known"] == 0
    host = metrics_swarm.host_row(SLUG, NOW, HostSample(0.0, 1, 0, 0), {})
    assert host["held_spawns"] == 0 and host["reason"] == ""
    call = {
        "_source_id": "record",
        "ts": "2027-01-15T08:00:00+00:00",
        "purpose": "intent",
        "source": None,
        "latency_ms": 1,
        "answers": {"z": {"value": False}, "a": {"value": True}},
    }
    row = metrics_swarm.classifier_rows([call], "machine")[0]
    assert row["ts_ms"] == NOW
    assert row["verdict"] == '{"a": {"value": true}, "z": {"value": false}}'
    assert row["backend"] == ""


def test_active_and_recently_completed_pulls_are_selected_with_a_bounded_window(box):
    tasks = [
        {**TASK, "id": name, "pr_url": name, "state": state}
        for name, state in (("pr", "pr"), ("claimed", "claimed"), ("done", "done"), ("stale", "done"), ("open", "open"))
    ]
    tasks.append({"id": "without", "state": "claimed"})
    events = [
        {"kind": "task done", "target": "tasks/done", "at": NOW - metrics_outbox.DAY_MS},
        {"kind": "task done", "target": "tasks/stale", "at": NOW - metrics_outbox.DAY_MS - 1},
        {"kind": "task claimed", "target": "tasks/open", "at": NOW},
    ]
    state = {"tasks": tasks, "_meta": {"events": events}}
    snapshots = []
    source = metrics_swarm.TickInput(None, state, [], lambda url: snapshots.append(url) or url)
    assert metrics_swarm.pull_rows(box, NOW, source) == {"pr": "pr", "claimed": "claimed", "done": "done"}
    assert snapshots == ["pr", "claimed", "done"]
    assert metrics_swarm.pull_rows(box, NOW, metrics_swarm.TickInput(None, {"tasks": []}, [], None)) == {}
    assert metrics_swarm.pull_rows(box, NOW, metrics_swarm.TickInput(None, {"tasks": [], "_meta": {}}, [], None)) == {}


def test_log_readers_preserve_empty_batches_and_skip_pending_reviews(box, tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_swarm.decision_log, "log_path", lambda: tmp_path / "absent")
    assert metrics_swarm.read_classifier_calls(box) == metrics_swarm.LogBatch("", 0, [])
    monkeypatch.setattr(metrics_swarm.gate_log, "swarm_home", lambda: tmp_path)
    assert metrics_swarm.read_review_events(SLUG, box) == metrics_swarm.LogBatch("", 0, [])
    path = tmp_path / SLUG / "gates" / "intent" / "history.jsonl"
    path.parent.mkdir(parents=True)
    reviews = [{"id": "pending", "submittedAt": None}, {"id": "r", "submittedAt": "2027-01-15T08:00:00+00:00"}]
    states = [{}, {"reviewer_findings": {}}, {"reviewer_findings": {"reviews": reviews}}]
    path.write_text(
        "".join(json.dumps({"task": "t", "classifier_input": json.dumps({"state": state})}) + "\n" for state in states)
    )
    batch = metrics_swarm.read_review_events(SLUG, box)
    assert batch.rows == [{"kind": "review round", "target": "tasks/t", "at": NOW, "review_id": "r"}]
    assert batch.position == path.stat().st_size


def test_all_row_families_enter_the_outbox_and_replay_without_loss(box, tmp_path, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 4, 0))
    old = [
        {**AGENT, "name": f"old{i}", "started_at": NOW - 10 - i, "ended_at": NOW - 5, "reason": "retired"}
        for i in range(3)
    ]
    for row in old:
        store.redis.rpush(store.key(SLUG, "history"), json.dumps(row))
    store.put_agent(SLUG, AgentRecord(**{**AGENT, "execution_id": ""}))
    transfer = {"id": "transfer", "at": NOW, "predecessor": "worker", "task": "t", "reason": "recycle"}
    store.redis.hset(store.key(SLUG, "transfers"), "transfer", json.dumps(transfer))
    quota = {
        "configured": {"eng": 4},
        "placements": {"eng": []},
        "reason": "held by quota",
        "accounts": [
            {"harness": "codex", "name": "a", "state": "OPEN", "five_left": 50.0, "week_left": None, "sessions": 1}
        ],
    }
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(quota))
    monkeypatch.setattr(metrics_swarm.gate_log, "swarm_home", lambda: tmp_path)
    gates = [
        {"at": NOW + i, "gate": "build", "kind": "deny", "agent": "worker", "task": "t", "reason": f"deny {i}"}
        for i in range(25)
    ]
    gates.append({"at": NOW + 26, "gate": "reruns", "kind": "count", "agent": "worker", "task": "t", "reason": "retry"})
    log = tmp_path / SLUG / "gates" / "log.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("".join(json.dumps(row) + "\n" for row in gates))
    review_log = log.parent / "intent" / "history.jsonl"
    review_log.parent.mkdir()
    state = {"reviewer_findings": {"reviews": [{"id": "r", "submittedAt": "2027-01-15T08:00:00+00:00"}]}}
    review_log.write_text(json.dumps({"task": "t", "classifier_input": json.dumps({"state": state})}) + "\n")
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(
        json.dumps(
            {
                "ts": "2027-01-15T08:00:00+00:00",
                "purpose": "intent",
                "source": "api",
                "latency_ms": 3,
                "answers": {"ready": True},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(metrics_swarm.decision_log, "log_path", lambda: decisions)
    monkeypatch.setattr(metrics_swarm.host_budget, "read_host", lambda: HostSample(6.0, 3, 1024, 1))
    monkeypatch.setattr(metrics_swarm.socket, "gethostname", lambda: "machine")
    ready = {lane: [] for lane in metrics_swarm.capacity.LANES}
    ready["eng"] = [{"id": "ready"}]
    monkeypatch.setattr(metrics_swarm.capacity, "ready_work", lambda *args: ({}, ready))
    ledger = doc([event("task claimed", NOW), event("task pr", NOW + 1)])
    found = [{"id": "finding", "kind": "idle with claim", "seen_at": NOW, "subject": "worker", "summary": "idle"}]
    metrics_swarm.record_pass(box, SLUG, NOW + 30, store, ledger, found, {})
    metrics_swarm.record_pass(box, SLUG, NOW + 30, store, ledger, found, {})
    agents = box.recent("agent_events", NOW + 30)
    assert {(row["kind"], row["finding_kind"]) for row in agents} == {
        ("spawn", ""),
        ("retire", ""),
        ("claim", ""),
        ("gate_deny", ""),
        ("handoff", ""),
        ("health_finding", "idle with claim"),
    }
    assert sum(row["kind"] == "spawn" for row in agents) == 4
    assert sum(row["kind"] == "retire" for row in agents) == 3
    assert sum(row["kind"] == "gate_deny" for row in agents) == 25
    assert sum(row["kind"] == "handoff" for row in agents) == 1
    assert sum(row["kind"] == "health_finding" for row in agents) == 1
    for row in agents:
        assert_node(row)
    deliveries = box.recent("delivery_events", NOW + 30)
    assert sorted(row["kind"] for row in deliveries) == ["pull_request_opened", "rerun", "review_round"]
    assert next(row for row in deliveries if row["kind"] == "rerun")["reason"] == "retry"
    for row in deliveries:
        assert_node(row)
        assert row["agent"] == "worker"
    host = box.recent("host_samples", NOW + 30)[0]
    assert [host[key] for key in ("held_spawns", "load_per_cpu", "available_mb", "reason")] == [
        1,
        2.0,
        1024,
        "held by quota",
    ]
    quota_row = box.recent("quota_samples", NOW + 30)[0]
    assert quota_row["ledger"] == SLUG and quota_row["ts_ms"] == NOW + 30
    assert quota_row["five_left"] == 50.0 and quota_row["week_left"] == -1.0
    calls = box.recent("classifier_calls", NOW + 30)
    assert len(calls) == 1
    assert [calls[0][key] for key in ("ledger", "host", "definition", "backend", "latency_ms")] == [
        "host:machine",
        "machine",
        "intent",
        "api",
        3,
    ]


def test_late_events_without_named_metadata_do_not_reuse_an_ended_owner():
    old = {**AGENT, "ended_at": NOW + 1, "reason": "retired"}
    claim = {key: value for key, value in event("task claimed", NOW + 4).items() if key != "by"}
    rows = metrics_swarm.agent_rows(SLUG, doc([claim]), [old])
    assert rows[-1]["agent"] == ""
    assert rows[1]["reason"] == "retired"
    unrelated = {**AGENT, "name": "XXXX", "task": "other", "started_at": NOW + 2}
    rows = metrics_swarm.delivery_rows(
        SLUG,
        doc(),
        [AGENT, unrelated],
        {TASK["pr_url"]: SimpleNamespace(state="MERGED", merged_at=NOW + 4)},
    )
    assert rows[0]["agent"] == "worker"
    active_claim = {key: value for key, value in event("task claimed", NOW + 4).items() if key != "by"}
    pr = {key: value for key, value in event("task pr", NOW + 4).items() if key != "by"}
    assert metrics_swarm.agent_rows(SLUG, doc([active_claim]), [AGENT, unrelated])[-1]["agent"] == "worker"
    assert metrics_swarm.delivery_rows(SLUG, doc([pr]), [AGENT, unrelated], {})[0]["agent"] == "worker"


def test_review_identity_ignores_the_snapshot_author():
    first = {**event("review round", NOW), "review_id": "r"}
    second = {**first, "by": "replacement"}
    rows = metrics_swarm.delivery_rows(SLUG, doc([first, second]), [AGENT], {})
    assert rows[0]["event_id"] == rows[1]["event_id"]


def test_controller_gate_and_handoff_events_keep_temporal_owner_and_distinct_ids():
    gates = [
        {"at": NOW + i, "gate": gate, "kind": "count", "agent": "controller", "task": "t", "reason": gate}
        for i, gate in enumerate(["idle-ticks", "idle-ticks", "reruns", "reruns"], 1)
    ]
    agents, delivery = metrics_swarm.gate_rows(SLUG, doc(), [AGENT], gates)
    assert len({row["event_id"] for row in agents}) == 2
    assert len({row["event_id"] for row in delivery}) == 2
    for row in agents + delivery:
        assert row["agent"] == "worker"
        assert_node(row)
    transfer = {"id": "t", "task": "t", "predecessor": "controller", "at": NOW + 1, "reason": "recycle"}
    assert metrics_swarm.signal_rows(SLUG, doc(), [AGENT], [transfer], [])[0]["agent"] == "worker"
    replacement = {**AGENT, "name": "replacement", "started_at": NOW + 1}
    transfer = {**transfer, "predecessor": "worker", "at": NOW + 2}
    assert metrics_swarm.signal_rows(SLUG, doc(), [AGENT, replacement], [transfer], [])[0]["agent"] == "worker"


def test_opened_pull_rows_do_not_suppress_merge_observation(box):
    opened = metrics_swarm.delivery_rows(SLUG, doc([event("task pr", NOW)]), [AGENT], {})
    box.append(metrics_swarm.DELIVERY, opened)
    calls = []
    source = metrics_swarm.TickInput(None, doc(), [], lambda url: calls.append(url) or url)
    assert metrics_swarm.pull_rows(box, NOW, source) == {TASK["pr_url"]: TASK["pr_url"]}
    assert calls == [TASK["pr_url"]]


def test_ready_tasks_in_an_unconfigured_lane_are_not_held_spawns(monkeypatch):
    ready = {lane: [] for lane in metrics_swarm.capacity.LANES}
    ready["eng"] = [{"id": "t"}]
    monkeypatch.setattr(metrics_swarm.capacity, "ready_work", lambda *args: ({}, ready))
    assert metrics_swarm._held_spawns(None, SLUG, doc(), {}, []) == 0


def test_local_window_includes_its_boundary_and_excludes_older_rows(box, tmp_path, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 0, 0))
    monkeypatch.setattr(metrics_swarm.host_budget, "read_host", lambda: HostSample(0.0, 1, 0, 0))
    monkeypatch.setattr(metrics_swarm.gate_log, "swarm_home", lambda: tmp_path)
    monkeypatch.setattr(metrics_swarm.decision_log, "log_path", lambda: tmp_path / "missing")
    cutoff = NOW - metrics_outbox.DAY_MS
    state = {"tasks": [], "_meta": {"events": [event("task claimed", cutoff), event("task claimed", cutoff - 1)]}}
    metrics_swarm.record_pass(box, SLUG, NOW, store, state, [], {})
    claims = [row for row in box.recent("agent_events", NOW) if row["kind"] == "claim"]
    assert len(claims) == 1 and claims[0]["ts_ms"] == cutoff
    metrics_swarm.record_pass(box, SLUG, NOW, store, {"tasks": []}, [], {})
    metrics_swarm.record_pass(box, SLUG, NOW, store, {"tasks": [], "_meta": {}}, [], {})
