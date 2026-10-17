import argparse

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", 2, 1, state="paused"))
    return saved


def test_a_new_swarm_has_the_master_login_off(store):
    assert store.config("sw").master_claude_login is False
    assert store.redis.hget(store.key("sw", "config"), "master_claude_login") == "False"


def test_a_swarm_saved_before_the_setting_reads_off(store):
    store.redis.hdel(store.key("sw", "config"), "master_claude_login")
    assert store.config("sw").master_claude_login is False


@pytest.mark.parametrize(("value", "saved"), [("on", True), ("off", False)])
def test_set_turns_the_master_login_on_and_off(store, value, saved):
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["master-claude-login=on"]))
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=[f"master-claude-login={value}"]))
    assert store.config("sw").master_claude_login is saved
    assert store.redis.hget(store.key("sw", "config"), "master_claude_login") == str(saved)


@pytest.mark.parametrize("value", ["yes", "true", "1", "ON", ""])
def test_set_refuses_any_other_value_without_saving(store, value):
    before = store.config("sw")
    with pytest.raises(SwarmError, match="^master-claude-login takes on or off$"):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=[f"master-claude-login={value}", "max-eng=5"]))
    assert store.config("sw") == before


def test_the_set_usage_names_the_setting(store):
    with pytest.raises(SwarmError, match=r"master-claude-login=on\|off"):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["no-such-key=1"]))
