from dataclasses import replace

from tests.test_hook_manager import quiet_session_start as quiet_session_start


def test_hive_hook_starts_and_ends_the_same_execution(monkeypatch, tmp_path):
    from fakeredis import FakeRedis

    from hooks.context import hive_join
    from scripts.hive import join_checks, join_report
    from scripts.swarm import store

    client = store.RedisStore(FakeRedis(decode_responses=True))
    client.create(store.SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    name = client.next_name("sw", "eng")
    agent = store.AgentRecord(name, "eng", "task", hive="worker", harness="claude", seat="eng-1@sw")
    agent = client.start_execution("sw", agent)
    client.put_agent("sw", agent)
    for name, value in {
        "AGENTIHOOKS_HIVE_ID": "worker",
        "AGENTIHOOKS_SWARM": "sw",
        "AGENTIHOOKS_AGENT_NAME": name,
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "HERDR_PANE_ID": "pane",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(store, "connect", lambda: client)
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    checks["brain"] = False
    monkeypatch.setattr(join_checks, "startup", lambda *args: checks)
    payload = {"session_id": "session", "transcript_path": str(tmp_path / "session.jsonl")}
    hive_join.run(payload, "start")
    report = join_report.read(client.redis, agent)
    assert report["grade"] == "partial"
    assert report["misses"] == {"brain": {"required": False}}
    assert report["context"]["home"] == str(tmp_path)
    assert report["context"]["pane"] == "pane"
    assert report["context"]["transcript_dir"] == str(tmp_path)
    assert report["started_at"] > 0
    assert report["session_id"] == "session"
    client.redis.hdel(client.key("sw", "agents"), agent.name)
    hive_join.run({**payload, "reason": "logout", "exit_status": 0}, "end")
    report = join_report.read(client.redis, agent)
    assert report["exit_reason"] == "logout"
    assert report["status"] == 0
    assert report["ended_at"] >= report["started_at"]
    assert join_report.read(client.redis, replace(agent, generation=agent.generation + 1)) == {}


def test_workstation_hook_does_not_connect(monkeypatch):
    from hooks.context import hive_join
    from scripts.swarm import store

    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)

    def unexpected():
        raise AssertionError("workstation must not probe hive access")

    monkeypatch.setattr(store, "connect", unexpected)
    assert hive_join.run({"session_id": "session"}, "start") is None


def test_hive_reports_are_dispatched_at_start_and_end(quiet_session_start, monkeypatch, tmp_path):
    from hooks import hook_manager
    from hooks.context import hive_join

    called = []
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(hive_join, "run", lambda *args: called.append(args))
    monkeypatch.setattr(hook_manager, "_request_trace_flush", lambda *args: None)
    payload = {"session_id": "session", "cwd": str(tmp_path)}
    hook_manager.EVENT_HANDLERS["SessionStart"](payload)
    hook_manager.EVENT_HANDLERS["SessionEnd"](payload)
    assert called == [(payload, "start"), (payload, "end")]


def test_hive_report_failure_does_not_abort_the_session_or_log_details(monkeypatch):
    from hooks import hook_manager
    from hooks.context import hive_join

    def refused(*args):
        raise ValueError("private detail")

    logged = []
    monkeypatch.setattr(hive_join, "run", refused)
    monkeypatch.setattr(hook_manager, "log", lambda *args: logged.append(args))
    hook_manager._hive_join({"session_id": "session"}, "start")
    assert logged == [("hive join report failed", {"error": "ValueError"})]


def test_hive_hook_reports_defaults_for_the_named_agent_once(monkeypatch):
    from fakeredis import FakeRedis

    from hooks.context import hive_join
    from scripts.hive import join_checks, join_report
    from scripts.swarm import store

    client = store.RedisStore(FakeRedis(decode_responses=True))
    client.create(store.SwarmConfig("sw", "/repo", max_eng=2, max_ci=0))
    seated = []
    for slot, task in enumerate(("other", "task"), start=1):
        record = store.AgentRecord(
            client.next_name("sw", "eng"),
            "eng",
            task,
            hive="worker",
            harness="copilot",
            profile="engineer",
            seat=f"eng-{slot}@sw",
            pane_id=f"{task}-pane",
        )
        seated.append(client.start_execution("sw", record))
        client.put_agent("sw", seated[-1])
    other, agent = seated
    for name, value in {
        "AGENTIHOOKS_HIVE_ID": "worker",
        "AGENTIHOOKS_SWARM": "sw",
        "AGENTIHOOKS_AGENT_NAME": agent.name,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setenv("XXXX", "/leak")
    monkeypatch.setattr(store, "connect", lambda: client)
    monkeypatch.setattr(hive_join.time, "time", lambda: 1792.5)
    calls = []
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    monkeypatch.setattr(join_checks, "startup", lambda redis, report: calls.append((redis, report)) or checks)
    hive_join.run({}, "start")
    hive_join.run({}, "start")
    context = {"home": "", "profile": "engineer", "harness": "copilot", "pane": "task-pane", "transcript_dir": ""}
    assert len(calls) == 1
    assert calls[0][0] is client.redis
    assert calls[0][1] == {
        "agent": agent.name,
        "slug": "sw",
        "execution_id": agent.execution_id,
        "started_at": 1792500,
        "context": context,
    }
    report = join_report.read(client.redis, agent)
    assert (report["session_id"], report["slug"], report["started_at"]) == ("", "sw", 1792500)
    assert join_report.read(client.redis, other) == {}
    hive_join.run({}, "end")
    report = join_report.read(client.redis, agent)
    assert (report["exit_reason"], report["status"], report["ended_at"]) == ("unknown", "ended", 1792500)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@nobody")
    assert hive_join.run({"session_id": "next"}, "start") is None
    assert len(calls) == 1
