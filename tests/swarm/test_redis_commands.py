import json

import pytest
import redis

from scripts.swarm import redis_commands
from scripts.swarm.keyspace import ROOT

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
    return redis_commands.Program(redis_commands.parse(tmp_path))


@pytest.fixture(scope="module")
def published():
    return redis_commands.consumer_map("distributed")["consumers"]


def test_a_redis_py_method_maps_to_the_command_it_sends():
    assert redis_commands.wire(redis.Redis, "incr") == {"INCRBY"}
    assert redis_commands.wire(redis.Redis, "blpop") == {"BLPOP"}
    assert redis_commands.wire(redis.Redis, "zscan_iter") == {"ZSCAN"}


def test_lua_scripts_name_the_commands_they_call():
    assert redis_commands.lua("return redis.call('incrby', KEYS[1], 1) + redis.call( \"GET\", KEYS[1])") == {
        "INCRBY",
        "GET",
    }


def test_only_commands_reached_from_the_entry_point_are_issued(program):
    reached = program.reach(("scripts.app:main",))
    issued = program.commands(reached)
    assert set(issued) == {"INCRBY", "BLPOP", "PEXPIRE"}
    assert issued["INCRBY"] == {"scripts.lease:acquire"}
    assert issued["BLPOP"] == {"scripts.timer:Timer.wait"}
    assert reached["scripts.timer:Timer.wait"] == "scripts.app:main"


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
