import argparse
import json

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError


def test_tick_mode_defaults_to_sequential() -> None:
    assert SwarmConfig("demo", ".", 1, 1).tick_mode == "sequential"


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_tick_mode_round_trips(mode: str) -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", ".", 1, 1))
    assert store.update("demo", tick_mode=mode).tick_mode == mode
    assert store.config("demo").tick_mode == mode


def test_missing_stored_tick_mode_remains_sequential() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", ".", 1, 1))
    store.redis.hdel(store.key("demo", "config"), "tick_mode")
    assert store.config("demo").tick_mode == "sequential"


@pytest.mark.parametrize("mode", ["", "unknown", "SEQUENTIAL"])
def test_invalid_tick_mode_is_rejected(mode: str) -> None:
    with pytest.raises(SwarmError) as raised:
        SwarmConfig("demo", ".", 1, 1, tick_mode=mode)
    assert str(raised.value) == "tick mode must be one of sequential, parallel"


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_set_command_accepts_tick_mode(mode: str, capsys: pytest.CaptureFixture[str]) -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", ".", 1, 1))
    cli.cmd_set(store, argparse.Namespace(slug="demo", pairs=[f"tick-mode={mode}"]))
    assert json.loads(capsys.readouterr().out)["tick_mode"] == mode
    assert store.config("demo").tick_mode == mode
