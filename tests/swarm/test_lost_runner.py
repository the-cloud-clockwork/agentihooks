import sys
from types import SimpleNamespace

import pytest

from scripts.swarm import lost_runner, tick
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger, FakeRuntime

NOW_MS = 1_792_430_000_000


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/test.yml").write_text("name: Tests\n")
    return SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True)), SimpleNamespace(repo=str(repo))


def spawned():
    calls = []
    return calls, lambda command, **kwargs: calls.append((command, kwargs))


def kubectl(name):
    return "/usr/bin/kubectl" if name == "kubectl" else None


def test_the_tick_starts_the_cluster_watchdog_in_the_repository_with_its_lock(swarm, tmp_path):
    store, config = swarm
    calls, popen = spawned()
    assert lost_runner.sweep("sw", config, store, NOW_MS, popen, kubectl) == []
    [(command, kwargs)] = calls
    assert command == [
        sys.executable,
        "-I",
        "-m",
        "scripts.ci_lost_runner",
        "--repo",
        "{owner}/{repo}",
        "--cluster",
        "--lock",
        str(tmp_path / "home/lost-runner.lock"),
    ]
    assert kwargs["cwd"] == config.repo
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] is kwargs["stdout"]
    assert kwargs["stdout"].name == str(tmp_path / "home/lost-runner.log")


def test_every_swarm_on_one_repository_starts_it_at_most_once_a_minute(swarm):
    store, config = swarm
    calls, popen = spawned()
    lost_runner.sweep("sw", config, store, NOW_MS, popen, kubectl)
    lost_runner.sweep("other", config, store, NOW_MS + 1000, popen, kubectl)
    assert len(calls) == 1
    assert 0 < store.redis.pttl(f"{PREFIX}:lost-runner:{config.repo}") <= lost_runner.INTERVAL_MS
    store.redis.delete(f"{PREFIX}:lost-runner:{config.repo}")
    lost_runner.sweep("sw", config, store, NOW_MS + lost_runner.INTERVAL_MS, popen, kubectl)
    assert len(calls) == 2


def test_a_host_without_kubectl_starts_nothing(swarm):
    store, config = swarm
    calls, popen = spawned()
    assert lost_runner.sweep("sw", config, store, NOW_MS, popen, lambda name: None) == []
    assert calls == []


@pytest.mark.parametrize("repo", ["", "missing"])
def test_a_repository_without_the_tests_workflow_starts_nothing(swarm, tmp_path, repo):
    store, _ = swarm
    calls, popen = spawned()
    config = SimpleNamespace(repo=repo and str(tmp_path / repo))
    assert lost_runner.sweep("sw", config, store, NOW_MS, popen, kubectl) == []
    assert calls == []


def test_a_host_without_the_agentihooks_home_starts_nothing_and_keeps_the_minute(swarm, tmp_path):
    store, config = swarm
    (tmp_path / "home").rmdir()
    calls, popen = spawned()
    assert lost_runner.sweep("sw", config, store, NOW_MS, popen, kubectl) == []
    assert calls == []
    assert store.redis.get(f"{PREFIX}:lost-runner:{config.repo}") is None


def test_the_home_defaults_to_the_agentihooks_folder_in_the_user_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    monkeypatch.setattr(lost_runner.Path, "home", lambda: tmp_path)
    assert lost_runner.home() == tmp_path / ".agentihooks"


def test_every_tick_runs_the_sweep_with_its_swarm_config(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    calls = []
    monkeypatch.setattr(lost_runner, "sweep", lambda *args: calls.append(args) or [])
    tick.tick("sw", store, FakeLedger([]), FakeRuntime(), now_ms=1_000)
    [(slug, config, swept, now_ms)] = calls
    assert (slug, config.repo, swept, now_ms) == ("sw", "/repo", store, 1_000)
