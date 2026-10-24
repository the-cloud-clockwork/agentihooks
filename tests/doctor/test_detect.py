from types import SimpleNamespace

import pytest

from scripts.doctor import detect
from scripts.swarm import ledger_events
from scripts.swarm.health.findings import Finding
from tests.doctor.recorded import load

STALE = Finding("stale claim", "watch-eng-1", "no change for 40 minutes", ("task t1",), "30 minutes", 40)


def test_a_failing_detector_is_named_and_the_others_still_report():
    def broken():
        raise RuntimeError("journal unreadable")

    found, failed = detect.collect({"spawn": broken, "health": lambda: [STALE]})
    assert found == [STALE]
    assert failed == ["the spawn detector failed: RuntimeError: journal unreadable"]


def test_detector_records_its_own_stage_and_keeps_running_after_failure(capsys):
    from scripts.swarm import timing

    def broken():
        raise RuntimeError("journal unreadable")

    with timing.tick("doctor"):
        assert detect.collect({"spawn": broken, "health": lambda: [STALE], "trace": lambda: [STALE]}) == (
            [STALE, STALE],
            ["the spawn detector failed: RuntimeError: journal unreadable"],
        )
    import json

    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    steps = {}
    for row in rows:
        steps.setdefault(row["step"], []).append((row["phase"], row.get("outcome")))
    assert steps == {
        "doctor.spawn": [("started", None), ("finished", "error")],
        "doctor.health": [("started", None), ("finished", "success")],
        "doctor.trace": [("started", None), ("finished", "success")],
    }
    assert all(row["slug"] == "doctor" for row in rows)
    assert all(
        row[key] >= 0
        for row in rows
        if row["phase"] == "finished"
        for key in ("wall_s", "own_cpu_s", "reaped_child_cpu_s")
    )


def test_the_detectors_of_one_pass_run_side_by_side():
    import threading

    met = threading.Barrier(3, timeout=5)

    def waits():
        met.wait()
        return [STALE]

    assert detect.collect({"spawn": waits, "health": waits, "ci": waits}) == ([STALE, STALE, STALE], [])


def test_every_detector_of_a_wide_pass_runs_at_once():
    import threading

    met = threading.Barrier(40, timeout=5)

    def waits():
        met.wait()
        return [STALE]

    assert detect.collect({f"d{n}": waits for n in range(40)}) == ([STALE] * 40, [])


def test_a_pass_with_no_detectors_finds_nothing():
    assert detect.collect({}) == ([], [])


def test_the_spawn_reader_reads_the_watched_swarm_at_the_pass_time(monkeypatch):
    from scripts.doctor import spawn_read, spawns

    seen = []
    monkeypatch.setattr(spawn_read, "records", lambda store, slug, now_ms: seen.append((store, slug, now_ms)) or "rec")
    monkeypatch.setattr(spawns, "findings", lambda record: [STALE] if record == "rec" else [])
    import fakeredis

    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    assert detect.readers(store, None, "sw", 42, environ={})["spawn"]() == [STALE]
    assert seen == [(store, "sw", 42)]


def test_the_master_launch_reader_reads_the_watched_swarm(monkeypatch):
    from scripts.doctor import master_launches, spawn_read

    seen = []
    monkeypatch.setattr(spawn_read, "master_records", lambda store, slug: seen.append((store, slug)) or "rec")
    monkeypatch.setattr(master_launches, "findings", lambda record: [STALE] if record == "rec" else [])
    import fakeredis

    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    assert detect.readers(store, None, "sw", 42, environ={})["master launch"]() == [STALE]
    assert seen == [(store, "sw")]


@pytest.mark.parametrize(("configured", "limit"), [(0, 600), (700, 700)])
def test_the_context_overrun_reader_reads_the_swarm_agents_at_its_compact_limit(monkeypatch, configured, limit):
    import fakeredis

    from hooks.context import context_recycle
    from scripts.doctor import context_overruns
    from scripts.swarm.store import AgentRecord

    asked = []
    monkeypatch.setenv("AGENTIHOOKS_COMPACT_LIMIT", "600")
    monkeypatch.setattr(
        context_recycle, "unmarked_overrun", lambda session, compact: asked.append((session, compact)) or 1
    )

    def found(agents, overrun):
        assert [(a["name"], a["conversation_id"]) for a in agents] == [("master@323133-0175", "c1")]
        return [STALE] if overrun("c1") == 1 else []

    monkeypatch.setattr(context_overruns, "findings", found)
    agent = AgentRecord("master@323133-0175", "master", "master", conversation_id="c1")
    store = SimpleNamespace(
        redis=fakeredis.FakeRedis(decode_responses=True),
        config=lambda slug: SimpleNamespace(compact_limit=configured if slug == "sw" else 1),
        agents=lambda slug: [agent] if slug == "sw" else [],
    )
    assert detect.readers(store, None, "sw", 42, environ={})["context overrun"]() == [STALE]
    assert asked == [("c1", limit)]


def test_the_inbox_reader_holds_delivered_mail_to_its_receiver_and_leaves_out_outside_sessions():
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import AgentRecord, RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    box = InboxStore(store.redis)
    store.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task="t1"))
    worked = box.send("sw-eng-3", "sw-eng-1", "push your branch")
    box.deliver(worked.id, "sw-eng-1")
    left = box.send("sw-eng-3", "sw-eng-2", "push your branch")
    box.deliver(left.id, "sw-eng-2")
    box.send("sw-eng-3", "engineer-100001-0001-tmp-1", "reply with the word")
    later = worked.created_at + 10 * 60_000
    found = detect.readers(store, None, "sw", later, environ={})["inbox"]()
    assert [f.id for f in found] == [f"inbox-past-window/{left.id}"]


def test_the_startup_reader_judges_hive_agents_from_their_heartbeat_after_the_acknowledgement():
    import fakeredis

    from scripts.doctor import spawns
    from scripts.swarm import idle
    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import ACKNOWLEDGING

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    acked, timings = 50_000, {"returned_at": 50_000}
    hive = {"started_at": 1_000, "hive": "h1", "launch_timings": timings}
    running = AgentRecord("sw-eng-1", "eng", "t1", **hive)
    silent = AgentRecord("sw-eng-2", "eng", "t2", **hive)
    waiting = AgentRecord("sw-eng-3", "eng", "t3", started_at=1_000, state="starting")
    local = AgentRecord("sw-eng-4", "eng", "t4", started_at=1_000)
    store.agents = lambda slug: [running, silent, waiting, local] if slug == "sw" else []
    store.record_launch("sw", waiting, ACKNOWLEDGING)
    store.record_launch("sw", running, "started")
    idle.beat(store.redis, "sw", running.name, idle.WORKING, acked + 1)
    idle.beat(store.redis, "sw", local.name, idle.WORKING, 2_000)
    found = detect.readers(store, None, "sw", acked + 2 * spawns.TICK_MS, environ={})["startup"]()
    assert sorted(f.subject for f in found) == ["sw-eng-2", "sw-eng-4"]


def test_ci_reads_only_the_pull_requests_of_tasks_waiting_in_review():
    tasks = [
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/9"},
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/9"},
        {"state": "done", "pr_url": "https://github.com/o/r/pull/3"},
        {"state": "pr", "pr_url": ""},
        {"state": "pr", "pr_url": "https://github.com/o/other/pull/12"},
    ]
    assert detect.open_pulls(tasks) == [("o/other", 12), ("o/r", 9)]


def test_ci_reports_a_red_head_only_past_the_tick_red_window(monkeypatch):
    record = load("ci")
    record["checks"][0]["conclusion"] = "failure"
    monkeypatch.setattr(
        detect.ci_read,
        "pull_request",
        lambda repo, number, cache: {("o/r", 487, store.redis): record}[(repo, number, cache)],
    )
    ledger = SimpleNamespace(tasks=lambda slug: [{"state": "pr", "pr_url": "https://github.com/o/r/pull/487"}])
    window = ledger_events.RED_QUIET_MS
    import fakeredis

    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    red = ledger_events.iso_ms(record["checks"][0]["completed_at"])
    early = detect.readers(store, ledger, "s", red + window - 1, environ={})["ci"]()
    late = detect.readers(store, ledger, "s", red + window, environ={})["ci"]()
    assert early == []
    assert [f.id for f in late] == ["red-checks/487"]


def test_ci_reads_the_pull_requests_of_one_pass_side_by_side(monkeypatch):
    import threading

    import fakeredis

    met = threading.Barrier(2, timeout=5)

    def pull_request(repo, number, cache):
        met.wait()
        return {"number": number}

    monkeypatch.setattr(detect.ci_read, "pull_request", pull_request)
    monkeypatch.setattr(detect.ci, "findings", lambda record, now_ms: [record["number"]])
    tasks = [{"state": "pr", "pr_url": f"https://github.com/o/r/pull/{n}"} for n in (4, 5)]
    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    assert detect.readers(store, SimpleNamespace(tasks=lambda slug: tasks), "s", 1, environ={})["ci"]() == [4, 5]


def test_one_pass_reads_the_watched_tasks_once_for_the_ci_and_trace_detectors(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore

    store, reads = RedisStore(fakeredis.FakeRedis(decode_responses=True)), []
    ledger = SimpleNamespace(tasks=lambda slug: reads.append(slug) or [])
    monkeypatch.setattr(detect.traces_read, "record", lambda slug, tasks, agents, now_ms, get: {"traces": []})
    monkeypatch.setattr(detect.traces, "findings", lambda record, limits: [])
    found = detect.readers(store, ledger, "sw", 1_000, environ={})
    assert found["ci"]() == []
    assert found["trace"]() == []
    assert reads == ["sw"]


def test_the_health_detector_judges_only_what_the_current_health_pass_reports(monkeypatch):
    import json

    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    record = {"seen_at": 0, "verdict": None, "returned": False, "evidence": ["task t1 (pr)"], "measure": 4}
    for agent in ("s-eng-1", "s-eng-2"):
        store.redis.hset(store.key("s", "findings"), f"idle-with-claim/{agent}", json.dumps(record))
    states = {"s": {"tasks": [{"id": "t1"}], "_meta": {"events": [{"kind": "x"}]}}, "bare": {}}
    ledger = type("Ledger", (), {"state": lambda self, slug: states[slug]})()
    seen = []

    def current(store_, slug, config, tasks, events):
        seen.append((store_, slug, config, tasks, events))
        return [{"id": "idle-with-claim/s-eng-2"}]

    monkeypatch.setattr(detect.status, "findings", current)
    monkeypatch.setattr(store, "config", lambda slug: f"config of {slug}")
    found = detect.readers(store, ledger, "s", 10**12, environ={})["health"]()
    assert [f.id for f in found] == ["unjudged-finding/idle-with-claim/s-eng-2"]
    assert seen == [(store, "s", "config of s", states["s"]["tasks"], states["s"]["_meta"]["events"])]
    assert detect.reported(store, ledger, "bare") == {"idle-with-claim/s-eng-2"}
    assert seen[-1] == (store, "bare", "config of bare", [], [])


def _canonical(call):
    import json

    return json.dumps(call, sort_keys=True)


def test_the_trace_detector_reads_the_swarm_tag_and_its_sessions(monkeypatch, tmp_path):
    from hooks.observability import agent_trace
    from scripts.doctor import registry, traces_read
    from scripts.swarm.store import AgentRecord

    asked = []

    def get(path, params):
        asked.append((path, params))
        return {"data": [], "meta": {"page": 1, "totalPages": 1}}

    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.agents = lambda slug: [AgentRecord("s-master-1", "master", "master", conversation_id="c1", started_at=1)]
    ledger = type("Ledger", (), {"tasks": lambda self, slug: []})()
    monkeypatch.setattr(traces_read, "client", lambda env: get)
    monkeypatch.setattr(traces_read, "SWARM_HOME", tmp_path)
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "agent_trace")
    found = detect.readers(store, ledger, "s", 10**12, environ={})["trace"]()
    assert sorted(asked, key=_canonical) == sorted(
        [
            (
                "traces",
                {
                    "tags": ["swarm:s", "agent:s-master-1"],
                    "fields": "core,io",
                    "orderBy": "timestamp.desc",
                    "limit": registry.TRACES_LIMIT,
                    "page": 1,
                },
            ),
            ("traces", {"tags": "swarm:s", "fields": "core", "page": 1, "limit": 100}),
        ],
        key=_canonical,
    )
    assert [f.id for f in found] == ["untraced-session/s-master-1", "telemetry-never-exported/s-master-1.1"]
    assert (tmp_path / "s" / traces_read.STATE_FILE).exists()


def test_doctor_detectors_share_one_mail_snapshot_per_pass(monkeypatch, tmp_path):
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    reads = []

    def items(mail, slug):
        reads.append(slug)
        return []

    monkeypatch.setattr(detect.read, "inbox_items", items)
    first = detect.readers(store, None, "sw", 1_000, environ={}, home=tmp_path)
    assert first["inbox"]() == []
    assert first["handoff"]() == []
    assert reads == ["sw"]
    second = detect.readers(store, None, "sw", 2_000, environ={}, home=tmp_path)
    assert second["handoff"]() == []
    assert second["inbox"]() == []
    assert reads == ["sw", "sw"]


def test_a_pass_without_telemetry_drops_only_the_trace_reader():
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    every = set(detect.readers(store, None, "sw", 1_000, environ={}))
    assert "trace" in every
    assert set(detect.readers(store, None, "sw", 1_000, environ={}, telemetry=False)) == every - {"trace"}


def test_the_dev_workflow_reader_judges_the_watched_repo_dev_pushes(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    path = ".github/workflows/hive-worker.yml"
    runs = [
        {"id": n, "name": "Worker hive pod", "path": path, "conclusion": "failure", "created_at": "t", "html_url": "u"}
        for n in range(10, 15)
    ]
    record = {"runs": runs, "green": {}, "jobs": {}}
    monkeypatch.setattr(detect.ci_read, "dev_pushes", {"/repo": record}.get)
    reader = detect.readers(store, None, "sw", 1, environ={}).get("dev workflow", lambda: [])
    assert [f.id for f in reader()] == ["red-dev-workflow/hive-worker.yml"]
    record["runs"].insert(0, {**runs[0], "id": 15, "conclusion": "success"})
    assert reader() == []
