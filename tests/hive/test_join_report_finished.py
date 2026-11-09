"""A finished hive agent keeps the launch grade its join report earned."""

import pytest

from tests.hive.test_join_report import passing


@pytest.fixture(autouse=True)
def _every_running_report_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import join_report

    monkeypatch.setattr(
        join_report,
        "running_executions",
        lambda redis, reports: {(report["slug"], report["execution_id"], report["generation"]) for report in reports},
    )


def _gone() -> dict:
    return {"transcript": False, "profile": False, "settings": False, "pane": False, "brain": False}


@pytest.mark.feature("hive-join-heartbeat-keeps-passed-grade")
def test_a_heartbeat_after_the_session_closed_keeps_the_passed_launch_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    join_report.start(redis, "swarm", agent, "session", passing(), {}, 1000)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: _gone())
    join_report.heartbeat(redis, "worker", 181000)
    report = join_report.read(redis, agent)
    assert report["heartbeat_at"] == 181000
    assert report["grade"] == "complete"
    assert report["misses"] == {}


@pytest.mark.feature("hive-join-heartbeat-keeps-passed-grade")
def test_a_heartbeat_still_recovers_a_check_missing_at_join(monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    join_report.start(redis, "swarm", agent, "session", {**passing(), "pane": False}, {}, 1000)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {"pane": True, "transcript": False})
    join_report.heartbeat(redis, "worker", 181000)
    report = join_report.read(redis, agent)
    assert report["grade"] == "complete"
    assert report["checks"]["pane"] is True
    assert report["checks"]["transcript"] is True
