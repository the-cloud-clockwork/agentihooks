"""Swarm Redis stores in the suite write under the suite's key prefix, and a write to a production key name fails."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.gates.progress import Progress
from scripts.inbox.seen import SeenMarks
from scripts.inbox.store import InboxStore
from scripts.swarm import store as swarm_store
from scripts.swarm.store import RedisStore, SwarmConfig
from tests import redis_key_guard, swarm_v2_isolation

pytestmark = pytest.mark.xdist_group("fakeredis")


def _client():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis(monkeypatch):
    client = _client()
    monkeypatch.setattr(swarm_store, "redis_client", lambda environ=None: client)
    return client


def test_every_swarm_store_writes_under_the_suite_prefix(redis):
    store = RedisStore(redis)
    store.create(SwarmConfig(slug="demo", repo="/repo", max_eng=1, max_ci=0))
    store.seats.occupy("eng-1@demo", "engineer@100001-0001", 1)
    store.memory.learn("eng-1@demo", "engineer@100001-0001", "a lesson because a reason", 1)
    store.culture.set("demo", "culture text")
    InboxStore(redis).send("master@demo", "eng-1@demo", "hello")
    Progress(redis, "demo").outcome("eng-1@demo", "pushed")
    SeenMarks(redis).mark("eng-1@demo", "demo:1:c1")

    keys = sorted(redis.scan_iter("*"))

    assert len(keys) > 5
    assert [key for key in keys if not key.startswith(f"{swarm_v2_isolation.RUN_PREFIX}:")] == []


def test_a_production_key_write_fails_the_test():
    client = _client()
    before = len(redis_key_guard.written)

    with pytest.raises(redis_key_guard.ProductionKey) as refused:
        client.hset("agentihooks:swarm:demo:config", "slug", "demo")
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert str(refused.value) == "a test wrote the production Redis key agentihooks:swarm:demo:config"
    assert caught == ["agentihooks:swarm:demo:config"]
    assert client.exists("agentihooks:swarm:demo:config") == 0


def test_a_queued_pipeline_write_to_a_production_key_fails_the_test():
    client = _client()
    before = len(redis_key_guard.written)

    with pytest.raises(redis_key_guard.ProductionKey):
        client.pipeline().set("ok", "1").delete("agentihooks:inbox:item:1")
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert caught == ["agentihooks:inbox:item:1"]


@pytest.mark.parametrize(
    "command",
    [
        ("XGROUP", "CREATE", "agentihooks:swarm:demo:events", "readers", "$", "MKSTREAM"),
        ("SUNIONSTORE", "agentihooks:swarm:demo:all", "suite:a"),
        ("SET", "agenticore:memory:x", "1"),
        ("SADD", "suite:index", "agentihooks:swarm:demo:config"),
    ],
    ids=["xgroup", "sunionstore", "agenticore", "value"],
)
def test_any_command_outside_the_reads_naming_a_production_key_fails(command):
    client = _client()
    before = len(redis_key_guard.written)

    with pytest.raises(redis_key_guard.ProductionKey):
        client.execute_command(*command)
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert caught == [next(arg for arg in command if str(arg).startswith(redis_key_guard.PRODUCTION))]


@pytest.mark.parametrize(
    "command",
    [
        ("EVAL", "return redis.call('SET', 'agentihooks:swarm:x', '1')", 0),
        ("FCALL", "set_it", 0),
    ],
    ids=["eval", "fcall"],
)
def test_a_script_fails_the_test_whatever_it_names(command):
    client = _client()
    before = len(redis_key_guard.written)

    with pytest.raises(redis_key_guard.ProductionKey) as refused:
        client.execute_command(*command)
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert str(refused.value) == f"a test sent {command[0]}, which can reach production keys the guard cannot read"
    assert caught == [command[0]]
    assert client.exists("agentihooks:swarm:x") == 0


def test_a_wipe_passes_on_fakeredis_and_fails_on_a_real_client():
    import redis

    before = len(redis_key_guard.written)
    _client().flushall()
    real = redis.Redis(unix_socket_path="/nonexistent/redis.sock")

    with pytest.raises(redis_key_guard.ProductionKey):
        real.flushdb()
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert caught == ["FLUSHDB"]


def test_a_watched_pipeline_write_to_a_production_key_fails_the_test():
    client = _client()
    before = len(redis_key_guard.written)

    with client.pipeline() as pipe:
        pipe.watch("suite:watched")
        with pytest.raises(redis_key_guard.ProductionKey):
            pipe.set("agentihooks:swarm:demo:config", "1")
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert caught == ["agentihooks:swarm:demo:config"]


def test_reads_and_suite_prefixed_writes_pass():
    client = _client()
    before = len(redis_key_guard.written)

    client.get("agentihooks:swarm:demo:config")
    client.keys("agentihooks:*")
    client.set(f"{swarm_v2_isolation.RUN_PREFIX}:swarm:demo:config", "demo")

    assert redis_key_guard.written[before:] == []


@pytest.mark.parametrize("disable_plugin_autoload", ["", "1"])
def test_a_test_that_swallows_the_refusal_still_fails(tmp_path, monkeypatch, disable_plugin_autoload):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", disable_plugin_autoload)
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_text("from tests.conftest import _production_redis_key_guard  # noqa: F401\n")
    (tmp_path / "test_planted.py").write_text(
        "import fakeredis\n\n\n"
        "def test_planted():\n"
        "    try:\n"
        "        fakeredis.FakeRedis().set('agentihooks:swarm:demo:config', '1')\n"
        "    except Exception:\n"
        "        pass\n"
    )
    environ = {**os.environ, "PYTHONPATH": str(root)}

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", str(tmp_path)],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
    )

    assert run.returncode == 1
    assert "1 passed, 1 error" in run.stdout
    assert "AssertionError: this test wrote a production swarm Redis key" in run.stdout
