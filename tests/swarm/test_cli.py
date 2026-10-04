import json
import subprocess

import fakeredis
import pytest

from scripts.swarm import cli, runtime, timer
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from tests.swarm.test_tick import FakeLedger, FakeRuntime


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}])
    ledger.said = []
    ledger.comments = []
    ledger.say = lambda slug, text, by=None: ledger.said.append((text, by))
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((task, text, by))
    rt = FakeRuntime()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: True)
    return store, ledger, rt


def run(*argv):
    return cli.main(list(argv))


def test_create_is_paused_then_start_spawns_and_status_lists(env, capsys):
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-ci-agents", "1") == 0
    assert store.config("sw").state == "paused" and rt.spawned == []
    assert run("sw", "start") == 0
    assert [s[1] for s in rt.spawned] == ["sw-eng-1", "sw-ci-1"]
    run("sw", "status")
    assert "sw-eng-1" in capsys.readouterr().out


def test_bare_pairs_set_the_caps(env):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "max-eng-agents=4", "max-ci-agents=0") == 0
    assert (store.config("sw").max_eng, store.config("sw").max_ci) == (4, 0)
    assert run("sw", "set", "max-eng-agents=x") == 1


def test_done_closes_the_task_and_marks_the_agent_finished(env, monkeypatch):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "sw-eng-1")
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["pr_url"]) == ("done", "https://github.com/o/r/pull/9")
    assert [a.state for a in store.agents("sw") if a.name == "sw-eng-1"] == ["finished"]
    assert store.claimant("sw", "t1") is None


def test_block_comments_parks_and_finishes(env):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "sw-ci-1", "block", "waiting on a token only the operator can create") == 0
    assert ledger.rows["t2"]["state"] == "blocked"
    assert ledger.comments == [("t2", "waiting on a token only the operator can create", "sw-ci-1")]


def test_say_addresses_and_strangers_are_refused(env, capsys):
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "sw-eng-1", "say", "the docs task is merged", "--to", "ci") == 0
    assert ledger.said == [("@ci the docs task is merged", "sw-eng-1")]
    assert run("sw", "--as", "stranger", "say", "hello") == 1
    assert "not an agent" in capsys.readouterr().err


def test_stop_now_terminates_and_reopens(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "stop", "--now") == 0
    assert sorted(rt.killed) == ["sw-ci-1", "sw-eng-1"]
    assert store.config("sw").state == "stopped" and store.agents("sw") == []
    assert ledger.rows["t1"]["state"] == "open"


def test_redis_down_is_a_clear_failure(monkeypatch, capsys):
    def down():
        raise SwarmError("Redis is unreachable; the swarm refuses to run without it")

    monkeypatch.setattr(cli, "connect", down)
    assert run("sw", "status") == 1
    assert "refuses to run" in capsys.readouterr().err


def test_a_concurrent_tick_is_skipped(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running")
    store.redis.set(store.key("sw", "tick-lock"), "1")
    assert cli.run_tick(store, "sw", ledger, rt) == ["another tick is running"]


def test_timer_units_and_enable(tmp_path):
    calls = []
    ok = timer.ensure(
        "/bin/agentihooks", tmp_path, run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)
    )
    assert ok and (tmp_path / "agentihooks-swarm.timer").exists()
    assert "ExecStart=/bin/agentihooks swarm tick" in (tmp_path / "agentihooks-swarm.service").read_text()
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"]


def test_runtime_spawns_through_init_agent_with_a_private_prompt(tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="pane_id=w3:p1\nstatus=started\naccount=acct\nagent=codex\n", stderr=""
        )

    rt = runtime.HerdrRuntime(home=tmp_path, run=fake_run)
    config = cli.SwarmConfig("sw", "/repo", 1, 1)
    placed = rt.spawn(config, "ci", "sw-ci-1", {"id": "t2", "title": "speed up the tests"})
    prompt_path = tmp_path / "sw" / "prompts" / "sw-ci-1.md"
    assert placed == runtime.Placed("w3:p1", "codex", "acct")
    assert seen[0][1:8] == ["init-agent", "--workspace", "swarm-sw", "--dir", "/repo", "--name", "sw-ci-1"]
    assert oct(prompt_path.stat().st_mode)[-3:] == "600"
    text = prompt_path.read_text()
    assert "speed up the tests" in text and "CI speed" in text and "agentihooks swarm sw done --pr" in text


def test_runtime_spawn_failure_names_the_reason(tmp_path):
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, stdout="status=failed\n", stderr="herdr: no server\n")

    rt = runtime.HerdrRuntime(home=tmp_path, run=fail)
    with pytest.raises(runtime.SpawnError, match="no server"):
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "sw-eng-1", {"id": "t1", "title": "x"})


def test_agent_record_round_trips_through_status_json(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-eng-9", "eng", "t1"))
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["agents"][0]["name"] == "sw-eng-9"


def test_agentihooks_dispatches_swarm(monkeypatch):
    import scripts.install as install

    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["agentihooks", "swarm", "list"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert (exc.value.code, seen) == (0, [["list"]])
