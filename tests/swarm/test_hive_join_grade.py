from dataclasses import replace

import pytest


def test_hive_launch_uses_report_and_keeps_brain_optional():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
    code = store.ensure_code("sw").code
    agent = AgentRecord(f"engineer@{code}-0001", "eng", "task", execution_id="execution", hive="worker")
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    checks["brain"] = False
    join_report.start(store.redis, "sw", agent, "session", checks, {}, 1000)
    found = launch_check.misses(store, "sw", agent, {}, {}, True, ["brain"])
    assert found == {"hive_brain": {"expected": "available", "actual": "missing"}}
    assert set(found) <= launch_check.REPORT_ONLY
    report = launch_check.record(store, "sw", agent, found, 2000, 1000)
    assert report["state"] == "reported"


def test_an_agent_placed_on_this_machine_hive_is_checked_as_local_without_a_join_report(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.swarm import commands, launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
    code = store.ensure_code("sw").code
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    agent = AgentRecord(f"master@{code}-0002", "master", "master", execution_id="execution", hive="root")
    assert "hive_join" not in launch_check.misses(store, "sw", agent, {}, {}, True, [])
    remote = replace(agent, hive="worker")
    assert "hive_join" in launch_check.misses(store, "sw", remote, {}, {}, True, [])


@pytest.mark.parametrize("missing", [None, "ledger_write", "settings", "pane"])
def test_hive_missing_required_evidence_is_enforced(missing):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
    code = store.ensure_code("sw").code
    agent = AgentRecord(f"engineer@{code}-0001", "eng", "task", execution_id="execution", hive="worker")
    if missing:
        checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
        checks[missing] = False
        join_report.start(store.redis, "sw", agent, "session", checks, {}, 1000)
    found = launch_check.misses(store, "sw", agent, {}, {}, True, [])
    assert set(found) == {f"hive_{missing}" if missing else "hive_join"}
    assert not set(found) <= launch_check.REPORT_ONLY
    assert join_report.read(store.redis, replace(agent, generation=3)) == {}


@pytest.mark.parametrize(("relaunch", "spent"), [(False, False), (True, True)])
@pytest.mark.parametrize("lane", ["eng", "master"])
def test_required_hive_failure_retires_without_relaunch_entitlement(monkeypatch, relaunch, spent, lane):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord(store.next_name("sw", lane), lane, "task", hive="worker", started_at=1000)
    launch_check.begin(store, "sw", agent, 1000, relaunch=relaunch)
    if spent:
        launch_check.mark_relaunched(store, "sw", "task")
    retired, notices = [], []
    runtime = SimpleNamespace(
        live_names=lambda: {agent.name}, retire=lambda agent, **kwargs: retired.append(agent.name) or True
    )
    ledger = SimpleNamespace(notify=lambda *args: notices.append(args))
    monkeypatch.setattr(controller, "_drop", lambda *args: " dropped")
    found = {"hive_settings": {"expected": "available", "actual": "missing"}}
    action = controller._failed_launch(
        "sw", store, ledger, runtime, {}, launch_check.Miss(agent, found, relaunch), 10000
    )
    assert retired == [agent.name]
    assert action == f"retired {agent.name} after its hive join failed dropped"
    assert launch_check.report(store, "sw", "task")["state"] == "failed"
    assert agent.name not in launch_check.pending(store, "sw")
    assert notices == (
        [("sw", "The master failed its launch check within a minute on settings. The failed hive agent was retired.")]
        if lane == "master"
        else []
    )


def test_optional_hive_miss_settles_admission_and_never_retires(monkeypatch):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", hive="worker", started_at=1000)
    settled = []
    monkeypatch.setattr(controller, "_settle_admission", lambda *args: settled.append(args))
    found = {"hive_brain": {"expected": "available", "actual": "missing"}}
    action = controller._failed_launch(
        "sw", store, SimpleNamespace(), SimpleNamespace(), {}, launch_check.Miss(agent, found, True), 10000
    )
    assert "reported only" in action
    assert settled == [("sw", settled[0][1], agent)]
    assert launch_check.report(store, "sw", "task")["state"] == "reported"


def test_missing_hive_report_and_multiword_checks_are_named_in_plain_words():
    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    assert launch_check.misses(store, "sw", agent, {}, {}, True, []) == {
        "hive_join": {"expected": "session join report", "actual": "missing"}
    }
    assert launch_check.told({"hive_ledger_write": {}}, "retired") == (
        "The master failed its launch check within a minute on ledger write. The failed hive agent was retired."
    )


def test_every_optional_hive_check_is_report_only():
    from scripts.hive import join_report
    from scripts.swarm import launch_check

    assert launch_check.REPORT_ONLY == {"late", *(f"hive_{name}" for name in join_report.OPTIONAL)}
    assert "hive_brain" in launch_check.REPORT_ONLY


def test_failed_hive_launch_retires_records_and_drops_that_agent(monkeypatch):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", hive="worker", started_at=1000)
    runtime, ledger, rows = SimpleNamespace(), SimpleNamespace(), {"task": {"state": "claimed"}}
    retired, dropped = [], []
    monkeypatch.setattr(controller, "_retire_launch", lambda *args: retired.append(args) or True)
    monkeypatch.setattr(controller, "_drop", lambda *args: dropped.append(args) or " dropped")
    found = {"hive_pane": {"expected": "available", "actual": "missing"}}
    controller._failed_launch("sw", store, ledger, runtime, rows, launch_check.Miss(agent, found, False), 10000)
    assert retired == [("sw", store, runtime, agent, 10000)]
    assert dropped == [("sw", store, ledger, rows, agent)]
    report = launch_check.report(store, "sw", "task")
    assert (report["at"], report["elapsed_ms"]) == (10000, 9000)


@pytest.mark.parametrize(
    ("hive", "found", "settles"),
    [("worker", {"late", "hive_brain"}, True), ("worker", {"hive_brain"}, True), ("", {"late"}, False)],
)
def test_report_only_misses_settle_only_hive_admission(monkeypatch, hive, found, settles):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", hive=hive, started_at=1000)
    runtime, settled = SimpleNamespace(), []
    monkeypatch.setattr(controller, "_settle_admission", lambda *args: settled.append(args))
    monkeypatch.setattr(controller, "_retire_launch", lambda *args: pytest.fail("report only misses never retire"))
    misses = dict.fromkeys(found, {"expected": "available", "actual": "missing"})
    action = controller._failed_launch(
        "sw", store, SimpleNamespace(), runtime, {}, launch_check.Miss(agent, misses, False), 10000
    )
    assert "reported only" in action
    assert settled == ([("sw", runtime, agent)] if settles else [])


def test_status_text_prints_each_hive_agent_join_grade(monkeypatch, capsys):
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import cli, status
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from tests.swarm.test_tick import FakeLedger

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/r", 1, 0))
    record = AgentRecord(
        store.next_name("sw", "eng", 1001), "eng", "t1", harness="claude", seat="eng-1@sw", hive="worker"
    )
    agent = store.start_execution("sw", record)
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    checks["brain"] = False
    join_report.start(store.redis, "sw", agent, "session", checks, {}, 1000)
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: {agent.name})
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    line = f"hive join  {agent.name}\tpartial\tmisses=brain (optional)\theartbeat=1000"
    assert line in capsys.readouterr().out.splitlines()


REFUSED = (
    "ledger sw: ledger server not answering on https://ledger:8765: <urlopen error [Errno 111] Connection refused>"
)


def _ledger_refused_launch(monkeypatch, tmp_path, message):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import commands, launch_check, outage
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    monkeypatch.setattr(launch_check, "bundled", lambda profile: False)
    monkeypatch.setattr(launch_check, "expected", lambda agent: [])
    monkeypatch.setattr(controller, "_settle_admission", lambda *args: None)
    monkeypatch.setattr(controller, "_drop", lambda *args: " dropped")
    retired = []
    monkeypatch.setattr(
        controller, "_retire_launch", lambda slug, store, runtime, agent, now: retired.append(now) or True
    )
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
    record = AgentRecord(
        store.next_name("sw", "eng", 1001), "eng", "task", seat="eng-1@sw", hive="worker", started_at=1000
    )
    agent = store.start_execution("sw", record)
    start = launch_check.session_started_at(agent)
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    checks["ledger_join"] = False
    checks["errors"] = {"ledger_join": {"error": "SwarmError", "status": None, "message": message}}
    join_report.start(store.redis, "sw", agent, "session", checks, {}, start)
    launch_check.begin(store, "sw", agent, start, relaunch=False)
    runtime = SimpleNamespace(bindings=lambda agents: {}, live_names=lambda: {agent.name})

    def tick(at):
        return controller._launch_checks("sw", store, SimpleNamespace(), runtime, {}, {}, at)

    return SimpleNamespace(store=store, agent=agent, start=start, retired=retired, tick=tick, checks=checks)


def test_a_join_refused_during_a_ledger_outage_holds_and_passes_once_the_outage_ends(monkeypatch, tmp_path):
    from scripts.hive import join_report
    from scripts.swarm import launch_check, outage

    launch = _ledger_refused_launch(monkeypatch, tmp_path, REFUSED)
    outage.observe("sw", "ledger", ["Ledger health probe failed"], launch.start + 5_000)
    outage.observe("sw", "ledger", [], launch.start + 50_000)
    due = launch.start + launch_check.DEADLINE_MS + 10_000
    assert launch.tick(due) == [f"{launch.agent.name} launch check held: the ledger was unavailable for its join"]
    assert launch.retired == []
    assert launch.agent.name in launch_check.pending(launch.store, "sw")
    recovered = {**launch.checks, "ledger_join": True}
    recovered.pop("errors")
    join_report.start(launch.store.redis, "sw", launch.agent, "retried", recovered, {}, due)
    assert launch.tick(due + 30_000)[0].startswith(f"{launch.agent.name} passed its launch check")
    assert launch.retired == []
    assert launch_check.report(launch.store, "sw", "task")["state"] == "passed"


def test_a_refused_join_that_persists_past_the_regrade_retires(monkeypatch, tmp_path):
    from scripts.swarm import launch_check

    launch = _ledger_refused_launch(monkeypatch, tmp_path, REFUSED)
    due = launch.start + launch_check.DEADLINE_MS + 10_000
    launch.tick(due)
    launch.tick(due + launch_check.DEADLINE_MS - 1)
    assert launch.retired == []
    action = launch.tick(due + launch_check.DEADLINE_MS)
    assert action == [f"retired {launch.agent.name} after its hive join failed dropped"]
    assert launch.retired == [due + launch_check.DEADLINE_MS]


def test_a_join_timeout_during_an_open_ledger_outage_holds_until_the_outage_limit(monkeypatch, tmp_path):
    from scripts.swarm import launch_check, outage

    launch = _ledger_refused_launch(monkeypatch, tmp_path, "ledger sw: TimeoutError: timed out")
    outage.observe("sw", "ledger", ["Ledger health probe failed"], launch.start + 5_000)
    due = launch.start + launch_check.DEADLINE_MS
    launch.tick(due)
    launch.tick(due + outage.HOLD_LIMIT_MS - 1)
    assert launch.retired == []
    assert launch.tick(due + outage.HOLD_LIMIT_MS) == [
        f"retired {launch.agent.name} after its hive join failed dropped"
    ]


def test_a_ledger_join_denied_by_the_server_retires_without_a_hold(monkeypatch, tmp_path):
    from scripts.swarm import launch_check

    launch = _ledger_refused_launch(monkeypatch, tmp_path, "ledger sw: server refused: 403 forbidden")
    due = launch.start + launch_check.DEADLINE_MS
    assert launch.tick(due) == [f"retired {launch.agent.name} after its hive join failed dropped"]
    assert launch.retired == [due]
