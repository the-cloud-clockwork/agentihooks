import json
from dataclasses import replace

import pytest

from scripts.swarm import cli
from scripts.swarm.store import SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit


def test_engine_defaults_to_legacy_for_new_and_old_swarms(env):  # noqa: F811
    store, _, _ = env
    assert SwarmConfig("sw", "/repo", 1, 1).engine == "legacy"
    run("sw", "create", "--repo", "/repo")
    assert store.config("sw").engine == "legacy"
    store.redis.hdel(store.key("sw", "config"), "engine")
    assert store.config("sw").engine == "legacy"


@pytest.mark.parametrize("engine", ["legacy", "events"])
def test_explicit_engine_round_trips_through_config(env, engine):  # noqa: F811
    store, _, _ = env
    store.create(replace(SwarmConfig("sw", "/repo", 1, 1), engine=engine))
    assert store.config("sw").engine == engine
    assert store.redis.hget(store.key("sw", "config"), "engine") == engine


def test_set_switches_only_the_named_swarm_and_reports_the_engine(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("other", "create", "--repo", "/repo")
    capsys.readouterr()
    for engine in ("events", "legacy"):
        assert run("sw", "set", f"engine={engine}") == 0
        assert store.config("sw").engine == engine
        assert json.loads(capsys.readouterr().out)["engine"] == engine
        assert store.config("other").engine == "legacy"
        assert cli.setting(store.config("sw"), "engine") == engine


@pytest.mark.parametrize("engine", ["", "event", "EVENTS", "unknown"])
def test_invalid_engine_is_rejected_without_changing_config(env, capsys, engine):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    before = store.redis.hgetall(store.key("sw", "config"))
    capsys.readouterr()
    assert run("sw", "set", f"engine={engine}") == 1
    assert capsys.readouterr().err.strip() == "swarm: engine must be one of legacy, events"
    assert store.redis.hgetall(store.key("sw", "config")) == before
    with pytest.raises(SwarmError) as caught:
        store.update("sw", engine=engine)
    assert str(caught.value) == "engine must be one of legacy, events"
    assert store.redis.hgetall(store.key("sw", "config")) == before
