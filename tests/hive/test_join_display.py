def test_hive_list_shows_each_agent_grade_and_every_miss(monkeypatch, capsys):
    from fakeredis import FakeRedis

    from scripts.hive import cli, join_report, registry
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(
        registry,
        "hives",
        lambda client: [{"id": "worker", "heartbeat_at": 1000, "excluded": [], "ui": False, "max_agents": 1}],
    )
    monkeypatch.setattr(registry, "live", lambda *args: True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    checks.update(brain=False, github=False)
    join_report.start(redis, "sw", agent, "session", checks, {}, 1000)
    cli._list()
    output = capsys.readouterr().out
    assert "engineer" in output
    assert "failed" in output
    assert "brain" in output
    assert "github" in output
    assert "heartbeat=1000" in output
    assert "  engineer\tfailed\tmisses=github (required), brain (optional)\theartbeat=1000" in output.splitlines()


def test_hive_list_leaves_out_ended_reports(monkeypatch, capsys):
    from fakeredis import FakeRedis

    from scripts.hive import cli, join_report, registry
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(
        registry,
        "hives",
        lambda client: [{"id": "worker", "heartbeat_at": 1000, "excluded": [], "ui": False, "max_agents": 1}],
    )
    monkeypatch.setattr(registry, "live", lambda *args: True)
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    for name in ("running", "gone"):
        agent = AgentRecord(name, "eng", "task", execution_id=name, hive="worker")
        join_report.start(redis, "sw", agent, "session", checks, {}, 1000)
    join_report.end(redis, "worker", "gone", "session", "agent gone from the swarm", "ended", 2000)
    cli._list()
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  ")]
    assert rows == ["  running\tcomplete\tmisses=none\theartbeat=1000"]
