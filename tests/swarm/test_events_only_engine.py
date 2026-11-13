"""Events is the only swarm engine: new, stored legacy and missing values all read as events."""

import pytest

from scripts.swarm.store import SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-engine-events-only")
def test_a_new_swarm_config_runs_on_the_events_engine() -> None:
    assert SwarmConfig("sw", "/repo", 1, 1).engine == "events"


@pytest.mark.feature("swarm-engine-events-only")
@pytest.mark.parametrize("stored", ["legacy", None])
def test_a_stored_legacy_or_missing_engine_reads_as_events(env: tuple, stored: str | None) -> None:  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    key = store.key("sw", "config")
    assert store.redis.hget(key, "engine") == "events"
    if stored is None:
        store.redis.hdel(key, "engine")
    else:
        store.redis.hset(key, "engine", stored)
    assert store.config("sw").engine == "events"


@pytest.mark.feature("swarm-engine-events-only")
def test_a_legacy_engine_is_refused_without_changing_config(env: tuple, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    before = store.redis.hgetall(store.key("sw", "config"))
    capsys.readouterr()
    assert run("sw", "set", "engine=legacy") == 1
    assert capsys.readouterr().err.strip() == "swarm: engine must be events"
    assert store.redis.hgetall(store.key("sw", "config")) == before
    with pytest.raises(SwarmError) as caught:
        SwarmConfig("sw", "/repo", 1, 1, engine="legacy")
    assert str(caught.value) == "engine must be events"


@pytest.mark.feature("swarm-engine-events-only")
def test_set_usage_names_only_the_events_engine(env: tuple, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    assert run("sw", "set", "unknown=value") == 1
    assert capsys.readouterr().err.endswith("=<value>, engine=events, tick-mode=sequential|parallel\n")
