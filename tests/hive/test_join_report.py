import json
from dataclasses import replace

import pytest


@pytest.fixture(autouse=True)
def _every_running_report_is_live(monkeypatch):
    from scripts.hive import join_report

    monkeypatch.setattr(
        join_report,
        "running_executions",
        lambda redis, reports: {(report["slug"], report["execution_id"], report["generation"]) for report in reports},
    )


def passing():
    return dict.fromkeys(
        (
            "ledger_join",
            "ledger_write",
            "redis_claim",
            "redis_release",
            "redis_status",
            "redis_verdict",
            "transcript",
            "github",
            "profile",
            "settings",
            "pane",
            "started_at",
            "brain",
        ),
        True,
    )


@pytest.mark.parametrize(
    ("missing", "grade"),
    [((), "complete"), (("brain",), "partial"), (("ledger_write",), "failed"), (("brain", "pane"), "failed")],
)
def test_grade_retains_every_miss(missing, grade):
    from scripts.hive import join_report

    checks = passing()
    checks.update(dict.fromkeys(missing, False))
    report = join_report.graded({"checks": checks})
    assert report["grade"] == grade
    assert report["misses"] == {name: {"required": name != "brain"} for name in missing}


def test_missing_required_check_cannot_grade_complete():
    from scripts.hive import join_report

    report = join_report.graded({"checks": {"brain": True}})
    assert report["grade"] == "failed"
    assert set(report["misses"]) == set(passing()) - {"brain"}


def test_report_is_execution_scoped_and_exit_is_preserved(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", generation=3, hive="worker")
    report = join_report.start(redis, "swarm", agent, "session", passing(), {"home": "/render"}, 1000)
    assert report["started_at"] == report["heartbeat_at"] == 1000
    assert report["session_id"] == "session"
    assert report["generation"] == 3
    assert report["task"] == "task"
    assert report["status"] == "running"
    assert join_report.read(redis, agent) == report
    assert join_report.read(redis, replace(agent, execution_id="next")) == {}
    ended = join_report.end(redis, "worker", "execution", "session", "logout", "exited", 2000)
    assert ended["exit_reason"] == "logout"
    assert ended["status"] == "exited"
    assert ended["ended_at"] == 2000

    def probe(*args):
        pytest.fail("ended reports must not be probed")

    monkeypatch.setattr(join_report, "cheap_checks", probe)
    join_report.heartbeat(redis, "worker", 500000)
    assert join_report.read(redis, agent) == ended
    assert json.loads(redis.hget(join_report.key("worker"), "execution")) == ended


def test_heartbeat_refreshes_cheap_checks_at_cadence(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    join_report.start(redis, "swarm", agent, "session", passing(), {}, 1000)
    calls = []
    monkeypatch.setattr(
        join_report, "cheap_checks", lambda *args: calls.append(args) or {"pane": False, "brain": False}
    )
    join_report.heartbeat(redis, "worker", 1001)
    assert calls == []
    join_report.heartbeat(redis, "worker", 181000)
    assert len(calls) == 1
    report = join_report.read(redis, agent)
    assert report["heartbeat_at"] == 181000
    assert report["grade"] == "failed"
    assert report["checks"]["github"] is True
    assert report["misses"] == {"pane": {"required": True}, "brain": {"required": False}}


def test_old_session_cannot_end_current_report():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    report = join_report.start(redis, "swarm", agent, "new", passing(), {}, 1000)
    assert join_report.end(redis, "worker", "execution", "old", "logout", "exited", 2000) == {}
    assert join_report.read(redis, agent) == report


@pytest.mark.parametrize("replacement", [False, True])
def test_exit_rechecks_session_identity_after_a_concurrent_write(monkeypatch, replacement):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    join_report.start(redis, "sw", agent, "old", passing(), {}, 1000)
    original = redis.pipeline
    changed = False

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def conflict(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                if replacement:
                    join_report.start(redis, "sw", agent, "new", passing(), {}, 1500)
                else:
                    redis.hset(join_report.key("worker"), "other", "{}")
            return execute(*args, **kwargs)

        pipe.execute = conflict
        return pipe

    monkeypatch.setattr(redis, "pipeline", pipeline)
    result = join_report.end(redis, "worker", "execution", "old", "logout", 0, 2000)
    current = join_report.read(redis, agent)
    assert changed
    if replacement:
        assert result == {}
        assert current["session_id"] == "new"
        assert current["status"] == "running"
        assert "ended_at" not in current
    else:
        assert current == result
        assert current["exit_reason"] == "logout"
        assert current["status"] == 0
        assert current["ended_at"] == 2000


def test_report_summary_retains_grade_misses_heartbeat_and_exit():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    checks = passing()
    checks["brain"] = False
    join_report.start(redis, "sw", agent, "session", checks, {}, 1000)
    join_report.end(redis, "worker", "execution", "session", "logout", 0, 2000)
    local = AgentRecord("local", "eng", "other")
    assert (
        join_report.text(redis, [local, agent])
        == "hive join  engineer\tpartial\tmisses=brain (optional)\theartbeat=1000\texit=logout\tstatus=0\n"
    )
    assert join_report.listing(redis, "worker") == [join_report.read(redis, agent)]


def test_real_cheap_heartbeat_preserves_expensive_start_evidence(tmp_path):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    context = {
        "home": str(tmp_path / "missing"),
        "profile": "engineer",
        "harness": "claude",
        "pane": "",
        "transcript_dir": "",
    }
    join_report.start(redis, "sw", agent, "session", passing(), context, 1000)
    join_report.heartbeat(redis, "worker", 181000)
    report = join_report.read(redis, agent)
    assert report["grade"] == "failed"
    assert report["heartbeat_at"] == 181000
    assert report["checks"]["github"] is True
    assert report["checks"]["ledger_join"] is True
    assert report["checks"]["ledger_write"] is True
    assert report["misses"] == {
        "transcript": {"required": True},
        "profile": {"required": True},
        "settings": {"required": True},
        "pane": {"required": True},
        "brain": {"required": False},
    }


def test_report_key_host_and_identity_edges():
    from fakeredis import FakeRedis

    from scripts.hive import join_report, registry
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    assert join_report.key("worker") == registry.key("worker") + ":join-reports"
    placed = AgentRecord("engineer", "eng", "task", runtime_target={"host_id": "worker"})
    assert join_report.host(placed) == "worker"
    assert join_report.host(AgentRecord("local", "eng", "task")) == ""
    redis.hset(join_report.key("worker"), "", json.dumps({"generation": 0, "session_id": "stale"}))
    assert join_report.read(redis, AgentRecord("engineer", "eng", "task", hive="worker")) == {}
    assert join_report.end(redis, "worker", "absent", "session", "logout", 0, 2000) == {}


def test_repeated_start_of_one_session_keeps_the_first_report():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    first = join_report.start(redis, "sw", agent, "session", passing(), {}, 1000)
    assert join_report.start(redis, "sw", agent, "session", {}, {}, 2000) == first
    assert join_report.read(redis, agent) == first


def test_summary_names_each_miss_with_its_kind_and_text_lists_every_agent():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    failed = join_report.graded(
        {"agent": "one", "heartbeat_at": 5, "checks": {**passing(), "pane": False, "brain": False}}
    )
    assert join_report.summary(failed) == "one\tfailed\tmisses=pane (required), brain (optional)\theartbeat=5"
    redis = FakeRedis(decode_responses=True)
    agents = [AgentRecord(name, "eng", name, execution_id=name, hive="worker") for name in ("one", "two")]
    for agent in agents:
        join_report.start(redis, "sw", agent, "session", passing(), {}, 1000)
    assert join_report.text(redis, agents) == (
        "hive join  one\tcomplete\tmisses=none\theartbeat=1000\nhive join  two\tcomplete\tmisses=none\theartbeat=1000\n"
    )


def test_heartbeat_waits_a_full_cadence_from_an_epoch_heartbeat(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    join_report.start(redis, "sw", agent, "session", passing(), {}, 1_000_000_000)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: pytest.fail("probed before its cadence"))
    join_report.heartbeat(redis, "worker", 1_000_000_001)
    assert join_report.read(redis, agent)["heartbeat_at"] == 1_000_000_000


def _two_running(redis):
    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    agents = [AgentRecord(name, "eng", name, execution_id=name, hive="worker") for name in ("one", "two")]
    for agent in agents:
        join_report.start(redis, "sw", agent, "session", passing(), {}, 1000)
    return agents


def test_heartbeat_skips_an_ended_report_and_refreshes_the_next(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report

    redis = FakeRedis(decode_responses=True)
    ended, running = _two_running(redis)
    join_report.end(redis, "worker", ended.execution_id, "session", "logout", 0, 2000)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {})
    join_report.heartbeat(redis, "worker", 181000)
    assert join_report.read(redis, ended)["heartbeat_at"] == 1000
    assert join_report.read(redis, running)["heartbeat_at"] == 181000


def test_heartbeat_leaves_a_report_changed_during_its_probe_and_refreshes_the_next(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report

    redis = FakeRedis(decode_responses=True)
    changed, running = _two_running(redis)

    def probe(client, report):
        if report["agent"] == changed.name:
            join_report.end(redis, "worker", changed.execution_id, "session", "logout", 0, 2000)
        return {}

    monkeypatch.setattr(join_report, "cheap_checks", probe)
    join_report.heartbeat(redis, "worker", 181000)
    assert join_report.read(redis, changed)["status"] == 0
    assert join_report.read(redis, changed)["heartbeat_at"] == 1000
    assert join_report.read(redis, running)["heartbeat_at"] == 181000


def test_heartbeat_yields_to_a_concurrent_write_and_refreshes_the_next(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report

    redis = FakeRedis(decode_responses=True)
    raced, running = _two_running(redis)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {})
    original = redis.pipeline
    raced_once = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def conflict(*args, **kwargs):
            if not raced_once:
                raced_once.append(True)
                join_report.end(redis, "worker", raced.execution_id, "session", "logout", 0, 2000)
            return execute(*args, **kwargs)

        pipe.execute = conflict
        return pipe

    monkeypatch.setattr(redis, "pipeline", pipeline)
    join_report.heartbeat(redis, "worker", 181000)
    assert join_report.read(redis, raced)["status"] == 0
    assert join_report.read(redis, raced)["heartbeat_at"] == 1000
    assert join_report.read(redis, running)["heartbeat_at"] == 181000


def test_heartbeat_skips_a_report_removed_during_its_probe(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report

    redis = FakeRedis(decode_responses=True)
    removed, running = _two_running(redis)

    def probe(client, report):
        if report["agent"] == removed.name:
            redis.hdel(join_report.key("worker"), removed.execution_id)
        return {}

    monkeypatch.setattr(join_report, "cheap_checks", probe)
    join_report.heartbeat(redis, "worker", 181000)
    assert join_report.read(redis, removed) == {}
    assert join_report.read(redis, running)["heartbeat_at"] == 181000
