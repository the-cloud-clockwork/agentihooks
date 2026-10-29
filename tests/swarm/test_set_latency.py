import json
from argparse import Namespace
from unittest.mock import Mock

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig


def test_setting_a_running_swarm_returns_without_running_a_tick(monkeypatch, capsys):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0, state="running"))
    tick = Mock(side_effect=AssertionError("settings must not wait for reconciliation"))
    monkeypatch.setattr(cli, "run_tick", tick)

    cli.cmd_set(store, Namespace(slug="fixture", pairs=["max-eng-agents=8"]))

    assert store.config("fixture").max_eng == 8
    reply = json.loads(capsys.readouterr().out)
    assert reply["max_eng"] == 8
    assert reply["applies"] == "next tick"
    tick.assert_not_called()
