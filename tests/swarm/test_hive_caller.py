from dataclasses import replace

import fakeredis

from scripts.hive import auth
from scripts.swarm import cli, idle
from scripts.swarm import store as store_module
from tests.swarm.test_cli import env, run  # noqa: F401


def test_wait_accepts_a_hive_placed_agent_through_its_current_member_redis(env, monkeypatch, tmp_path):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    agent = next(a for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001")
    store.put_agent("sw", replace(agent, hive="current"))
    current_url = auth._with_user("rediss://hive.example:6380/0", "hive-current", "p")
    (tmp_path / "hive.env").write_text(f"AGENTIHOOKS_HIVE_ID=current\nAGENTIHOOKS_HIVE_REDIS_URL={current_url}\n")
    for name, value in {
        "AGENTIHOOKS_HOME": str(tmp_path),
        "AGENTIHOOKS_DEPLOYMENT": "distributed",
        "AGENTIHOOKS_HIVE_ID": "left",
        "AGENTIHOOKS_HIVE_REDIS_URL": auth._with_user("rediss://old.example:6379/0", "hive-left", "p"),
    }.items():
        monkeypatch.setenv(name, value)
    left_redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store_module, "_pinged", lambda redis, url: store.redis if url == current_url else left_redis)
    monkeypatch.setattr(cli, "connect", store_module.connect)
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)

    assert [a.hive for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] == ["current"]
    assert run("sw", "--as", "engineer@a1b2c3-0001", "wait", "15", "--reason", "deploy run") == 0
    assert idle.wait(store.redis, "sw", "engineer@a1b2c3-0001")["until"] == 1_000 + 15 * 60_000
