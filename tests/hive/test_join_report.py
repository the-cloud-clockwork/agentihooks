import json
from dataclasses import replace

import pytest


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
    assert report["status"] == "running"
    assert join_report.read(redis, agent) == report
    assert join_report.read(redis, replace(agent, execution_id="next")) == {}
    ended = join_report.end(redis, agent, "session", "logout", "exited", 2000)
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
    assert join_report.end(redis, agent, "old", "logout", "exited", 2000) == {}
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
    result = join_report.end(redis, agent, "old", "logout", 0, 2000)
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
    join_report.end(redis, agent, "session", "logout", 0, 2000)
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
