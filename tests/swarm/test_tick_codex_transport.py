import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm import launch_job
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from scripts.swarm_v2.runtime import codex
from scripts.swarm_v2.runtime.routed import routed
from tests.swarm.profile_fixture import validated
from tests.swarm.test_tick import FakeLedger, FakeRuntime


def _complete_launch(runtime, job):
    spec = json.loads(Path(job["request"]).read_text())
    placed = runtime._launch(SwarmConfig(**spec["config"]), spec["lane"], name=spec["name"], **spec["launch"])
    Path(job["path"]).write_text(json.dumps({"placed": asdict(placed)}))


@pytest.mark.parametrize("transport,harness", [("legacy", "codex"), ("app_server", "codex"), ("app_server", "claude")])
@pytest.mark.parametrize("lane", ["eng", "master"])
def test_tick_selects_and_records_codex_transport(tmp_path, monkeypatch, transport, harness, lane):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(codex, "connect", lambda: store)
    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, codex_inbox_transport=transport)
    store.create(config)
    ledger = FakeLedger([{"id": "t1", "title": "Transport", "lane": "eng"}])
    fake = FakeRuntime()
    calls, admitted = [], []

    def run(argv, **kwargs):
        from scripts.routing import native_login

        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        calls.append(argv)
        name = argv[argv.index("--name") + 1]
        fake.live.add(name)
        if harness == "codex":
            assert argv[argv.index("--codex-transport") + 1] == transport
            if transport == "app_server":
                seat = argv[argv.index("--seat") + 1]
                admitted.append(
                    store.start_execution(
                        "sw",
                        AgentRecord(
                            name,
                            lane,
                            kwargs["env"]["AGENTIHOOKS_SWARM_TASK"],
                            harness="codex",
                            seat=seat,
                            started_at=1100,
                            transport=transport,
                            runtime_target={"pid": 123, "pid_start": 456},
                        ),
                    )
                )
        else:
            assert "--codex-transport" not in argv
        output = f"status=started\nroute_status=routed\npane_id=w1:p2\nagent={harness}\n"
        return SimpleNamespace(
            returncode=0, stdout=validated(argv, output).replace('"pid": 123', f'"pid": {os.getpid()}'), stderr=""
        )

    monkeypatch.setattr(launch_job, "spawn", lambda job: _complete_launch(herdr, job))
    selected_lane = lane
    herdr = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "open"))
    fake.spawn = lambda config, lane, name, task: (
        FakeRuntime.spawn(fake, config, lane, name, task)
        if lane != selected_lane
        else herdr.spawn(config, lane, name, task)
    )
    actions = tick("sw", store, ledger, routed({}, herdr=fake), now_ms=1000)
    if lane == "eng":
        assert any(action.startswith("launching engineer") for action in actions), actions
        actions = tick("sw", store, ledger, routed({}, herdr=fake), now_ms=2000)
    prefix = "spawned master" if lane == "master" else "spawned engineer"
    assert any(action.startswith(prefix) for action in actions), actions
    worker = next(agent for agent in store.agents("sw") if agent.lane == lane)
    assert len(calls) == 1
    assert worker.transport == (transport if harness == "codex" else "")
    if harness == "codex":
        execution = store.execution("sw", worker.execution_id)
        assert execution.transport == transport
        assert execution.pane_id == "w1:p2"
        assert execution.state == "working"
        projection = json.loads(store.redis.hget(store.key("sw", "agents"), worker.name))
        assert "transport" not in projection
        assert execution.name == worker.name
        assert execution.seat == worker.seat
        if admitted:
            assert worker.execution_id == admitted[0].execution_id
            assert worker.generation == admitted[0].generation
            assert worker.started_at == admitted[0].started_at
            assert worker.runtime_target == admitted[0].runtime_target


@pytest.mark.parametrize("lane", ["eng", "master"])
@pytest.mark.parametrize("transport", ["legacy", "app_server"])
def test_failed_transport_admission_retires_the_launched_pane(tmp_path, monkeypatch, lane, transport):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, codex_inbox_transport=transport))
    monkeypatch.setattr(codex, "connect", lambda: store)
    fake = FakeRuntime()
    ended = []

    def run(argv, **kwargs):
        from scripts.routing import native_login

        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        name = argv[argv.index("--name") + 1]
        fake.live.add(name)
        output = "status=started\nroute_status=routed\npane_id=w1:p2\nagent=codex\n"
        return SimpleNamespace(
            returncode=0, stdout=validated(argv, output).replace('"pid": 123', '"pid": 999999999'), stderr=""
        )

    def terminate(name):
        ended.append(name)
        fake.live.discard(name)

    herdr = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("codex", "open"))
    monkeypatch.setattr(herdr, "_terminate", terminate)
    monkeypatch.setattr(fake, "reap_name", terminate)
    monkeypatch.setattr(launch_job, "spawn", lambda job: _complete_launch(herdr, job))
    selected_lane = lane
    fake.spawn = lambda config, lane, name, task: (
        herdr.spawn(config, lane, name, task)
        if lane == selected_lane
        else FakeRuntime.spawn(fake, config, lane, name, task)
    )
    ledger = FakeLedger([{"id": "t1", "title": "Transport", "lane": "eng"}])
    actions = tick("sw", store, ledger, routed({}, herdr=fake), now_ms=1000)
    if lane == "eng":
        assert any(action.startswith("launching engineer") for action in actions), actions
        actions = tick("sw", store, ledger, routed({}, herdr=fake), now_ms=2000)
    assert any("spawn failed" in action for action in actions), actions
    assert not any(agent.lane == lane for agent in store.agents("sw"))
    assert not store.execution_occupants("sw")
    assert ended if transport == "app_server" else "w1:p2" in fake.closed
    if lane == "eng":
        assert ledger.rows["t1"]["state"] == "open"
        assert store.launches("sw")[-1]["pane_id"] == "w1:p2"


@pytest.mark.parametrize(
    "task,expected",
    [
        ({"seat": "eng-1@sw"}, []),
        ({"seat": "eng-1@sw", "previous_execution_id": "exe-old"}, ["--previous-execution", "exe-old"]),
        (
            {"seat": "eng-1@sw", "previous_execution_id": "exe-old", "admitted_execution_id": "exe-hive"},
            ["--execution", "exe-hive"],
        ),
    ],
)
def test_a_hive_admitted_execution_replaces_the_predecessor_in_the_launch_arguments(task, expected):
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, codex_inbox_transport="app_server")
    assert codex.arguments(config, "codex", task) == [
        "--codex-transport",
        "app_server",
        "--seat",
        "eng-1@sw",
        *expected,
    ]
