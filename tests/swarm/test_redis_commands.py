import json

import pytest
import redis

from scripts.swarm import code_reach, redis_commands
from scripts.swarm.keyspace import ROOT
from scripts.swarm.redis_wire import lua, wire

pytestmark = pytest.mark.unit

APP = """
from scripts.lease import acquire
from scripts.timer import Timer


def main():
    acquire()
    with Timer() as timer:
        timer.wait()


def admin(client):
    client.acl_setuser("member")
"""
LEASE = """
EXTEND = "return redis.call('PEXPIRE', KEYS[1], ARGV[1])"


def acquire(redis):
    redis.incr("lease")
"""
TIMER = """
class Timer:
    def __enter__(self):
        return self

    def wait(self):
        self.redis.blpop(self.key)


class Event:
    def wait(self):
        self.redis.lpop(self.key)
"""


@pytest.fixture
def program(tmp_path):
    package = tmp_path / "scripts"
    package.mkdir()
    for name, text in {"__init__": "", "app": APP, "lease": LEASE, "timer": TIMER}.items():
        (package / f"{name}.py").write_text(text)
    return code_reach.Program(code_reach.parse(tmp_path))


@pytest.fixture(scope="module")
def published():
    return redis_commands.consumer_map("distributed")["consumers"]


def test_a_redis_py_method_maps_to_the_command_it_sends():
    assert wire(redis.Redis, "incr") == {"INCRBY"}
    assert wire(redis.Redis, "blpop") == {"BLPOP"}
    assert wire(redis.Redis, "zscan_iter") == {"ZSCAN"}
    assert wire(redis.Redis, "pipeline") == frozenset()
    assert wire(redis.Redis, "from_url") == frozenset()


def test_a_method_no_sample_call_completes_is_refused(monkeypatch):
    def unsampled(self, key):
        raise TypeError(key)

    monkeypatch.setattr(redis.Redis, "unsampled", unsampled, raising=False)
    with pytest.raises(LookupError, match="Redis.unsampled"):
        wire(redis.Redis, "unsampled")


def test_lua_scripts_name_the_commands_they_call():
    assert lua("return redis.call('incrby', KEYS[1], 1) + redis.call( \"GET\", KEYS[1])") == {"INCRBY", "GET"}


def test_only_commands_reached_from_the_entry_point_are_issued(program):
    reached = program.reach(("scripts.app:main",))
    issued = program.commands(reached)
    assert set(issued) == {"INCRBY", "BLPOP", "PEXPIRE"}
    assert issued["INCRBY"] == {"scripts.lease:acquire"}
    assert issued["BLPOP"] == {"scripts.timer:Timer.wait"}
    assert reached["scripts.timer:Timer.wait"] == "scripts.app:main"


def test_an_entry_point_missing_from_the_code_is_refused(program):
    with pytest.raises(KeyError, match="scripts.app:gone"):
        program.reach(("scripts.app:main", "scripts.app:gone"))


def test_every_consumer_reaches_redis(published):
    assert all(consumer["commands"] for consumer in published.values())


def test_the_tick_reads_the_swarm_index_before_ticking_each_swarm(published):
    commands = published["tick"]["commands"]
    assert "scripts.swarm.store:RedisStore.slugs" in commands["SMEMBERS"]


def test_the_map_names_each_consumer_with_its_redis_role(published):
    roles = {name: consumer["role"] for name, consumer in published.items()}
    assert roles == {
        "controller": "controller",
        "tick": "controller",
        "ledger server": "ledger",
        "worker": "worker",
        "hooks": "worker",
    }


def test_the_controller_issues_the_lease_counter_and_the_scheduler_wait(published):
    commands = published["controller"]["commands"]
    assert {"INCRBY", "BLPOP"} <= commands.keys()
    assert "scripts.scheduler.process_timer:Timer.wait" in commands["BLPOP"]


def test_the_controller_scheduler_keys_sit_in_the_swarm_namespace(published):
    assert published["controller"]["keys"] == {
        "scheduler entries": f"{ROOT}:scheduler:probe:entries",
        "scheduler wake": f"{ROOT}:scheduler:probe:wake:probe",
    }


def test_administrator_commands_reach_no_consumer(published):
    for consumer in published.values():
        assert not {command for command in consumer["commands"] if command.startswith(("ACL", "CONFIG"))}


def test_main_prints_the_map_for_the_named_deployment(monkeypatch, capsys):
    monkeypatch.setattr(redis_commands, "consumer_map", lambda deployment: {"deployment": deployment})
    assert redis_commands.main(["--deployment", "cloud"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deployment": "cloud"}
