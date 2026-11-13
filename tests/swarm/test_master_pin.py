from dataclasses import replace

import pytest

from scripts import front_door_route
from scripts.routing.master_account import MasterAccount
from scripts.swarm import capacity, master_pin
from scripts.swarm.store import AgentRecord, SwarmConfig


def account(name, cap=3, sessions=0, left=90, harness="claude"):
    return capacity.Account(harness, name, capacity._state(cap, None), sessions, left, left, cap)


def recording_load(found):
    seen = []

    def load(env):
        seen.append(env)
        return found

    return load, seen


@pytest.mark.parametrize(
    ("declared", "profile", "state", "expected"),
    [
        ("warned", "master", "working", 3),
        ("", "master", "working", 2),
        ("open", "master", "working", 2),
        ("warned", "planner", "working", 2),
        ("warned", "master", "finished", 2),
    ],
)
def test_a_warned_master_pinned_to_its_declared_account_holds_no_seat(declared, profile, state, expected):
    lanes = {"master": {"agent": "claude", "account": declared}}
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=0, max_plan=0, lanes=lanes)
    master = AgentRecord("master", "master", "master", harness="claude", account="warned", state=state)
    observed = [account("open"), account("warned", sessions=1)]
    decision = capacity.calculate(
        config, observed, [replace(master, profile=profile)], warned={("claude", "warned"): "week"}
    )
    assert decision["effective"] == {"eng": expected, "ci": 0, "plan": 0}


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ([account("open")], 3),
        ([account("open"), capacity.Account("claude", "main", "UNKNOWN", 0, None, None)], 3),
        ([account("open"), capacity.Account("claude", "main", "UNKNOWN", 0, None, 40)], 2),
        ([account("open"), capacity.Account("claude", "main", "UNKNOWN", 0, 40, None)], 2),
        ([account("open"), account("main", cap=0, harness="codex")], 3),
        ([account("open"), account("main", cap=0)], 2),
    ],
)
def test_a_declared_master_account_without_a_quota_reading_holds_no_seat(observed, expected):
    lanes = {"master": {"agent": "claude", "account": "main"}}
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=0, max_plan=0, lanes=lanes)
    decision = capacity.calculate(config, observed, [])
    assert decision["effective"]["eng"] == expected


@pytest.mark.parametrize(("harness", "expected"), [("claude", True), ("codex", False)])
def test_only_the_front_door_master_on_the_declared_claude_account_is_pinned(monkeypatch, harness, expected):
    calls = []

    def check(profile, name, args):
        calls.append((profile, name, args))
        return ["--route", "claude-ai"]

    monkeypatch.setattr(front_door_route, "check", check)
    agent = AgentRecord("boss", "master", "master", harness=harness, account="main", profile="master", seat="")
    assert master_pin.pinned({"account": "main"}, [account("main")], [agent], "sw") is expected
    assert calls == ([("master", "master 1 · sw", [])] if expected else [])


def test_a_master_the_front_door_route_leaves_free_is_not_pinned(monkeypatch):
    monkeypatch.setattr(front_door_route, "check", lambda profile, name, args: args)
    agent = AgentRecord("boss", "master", "master", harness="claude", account="main", profile="master")
    assert not master_pin.pinned({"account": "main"}, [account("main")], [agent], "sw")


@pytest.mark.parametrize("error", ["redis", "os"])
def test_an_unreadable_master_account_declares_no_master(monkeypatch, error):
    from redis.exceptions import ConnectionError as RedisConnectionError

    def down(env):
        raise RedisConnectionError("down") if error == "redis" else OSError("down")

    monkeypatch.setattr(master_pin.master_account, "load", down)
    assert master_pin.declared({}) == ""


def test_an_undeclared_claude_master_declares_no_master(monkeypatch):
    load, _ = recording_load({"codex": MasterAccount("codex", "cx")})
    monkeypatch.setattr(master_pin.master_account, "load", load)
    assert master_pin.declared({}) == ""


def test_the_declared_claude_master_is_read_from_the_given_environment(monkeypatch):
    load, seen = recording_load({"claude": MasterAccount("claude", "main")})
    monkeypatch.setattr(master_pin.master_account, "load", load)
    environ = {"AGENTIHOOKS_HOME": "/home"}
    assert master_pin.declared(environ) == "main"
    assert seen == [environ]


@pytest.mark.parametrize(("lanes", "declared"), [({}, "main"), ({"master": {"agent": "claude"}}, "")])
def test_a_config_without_a_master_lane_or_declared_account_stays_unchanged(monkeypatch, lanes, declared):
    found = {"claude": MasterAccount("claude", declared)} if declared else {}
    load, _ = recording_load(found)
    monkeypatch.setattr(master_pin.master_account, "load", load)
    config = SwarmConfig("sw", "/repo", 1, 0, lanes=lanes)
    assert master_pin.with_declared(config, {}) is config


def test_the_declared_master_account_joins_the_master_lane(monkeypatch):
    load, seen = recording_load({"claude": MasterAccount("claude", "main")})
    monkeypatch.setattr(master_pin.master_account, "load", load)
    environ = {"AGENTIHOOKS_HOME": "/home"}
    config = SwarmConfig("sw", "/repo", 1, 0, lanes={"master": {"agent": "claude"}, "eng": {"agent": "codex"}})
    joined = master_pin.with_declared(config, environ)
    assert joined.lanes == {"master": {"agent": "claude", "account": "main"}, "eng": {"agent": "codex"}}
    assert replace(joined, lanes=config.lanes) == config
    assert seen == [environ]
