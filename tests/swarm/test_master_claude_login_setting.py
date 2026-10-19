import argparse

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", 2, 1, state="paused"))
    return saved


def test_a_swarm_saved_with_the_old_setting_loads_without_it(store):
    store.redis.hset(store.key("sw", "config"), "master_claude_login", "True")
    assert not hasattr(store.config("sw"), "master_claude_login")


@pytest.mark.parametrize("value", ["on", "off"])
def test_set_no_longer_takes_the_master_login_key(store, value):
    before = store.config("sw")
    with pytest.raises(SwarmError, match="^set takes "):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=[f"master-claude-login={value}"]))
    assert store.config("sw") == before


def test_the_set_usage_no_longer_names_the_master_login(store):
    with pytest.raises(SwarmError) as refused:
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["no-such-key=1"]))
    assert "master-claude-login" not in str(refused.value)
