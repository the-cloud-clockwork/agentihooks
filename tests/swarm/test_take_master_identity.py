import json
import os
import sys

import pytest

from hooks.context import account_sessions, broadcast
from scripts.gates.base import Who
from scripts.gates.identity import refusal
from scripts.handoff import transfers
from scripts.swarm import cli, take_master
from scripts.swarm.store import AgentRecord
from scripts.swarm_ledger import ledger
from scripts.terminate_agent import sessions
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_take_master import DOC, taker  # noqa: F401
from tests.test_terminate_agent import process


@pytest.mark.parametrize("registered", [False, True, "alias"])
def test_a_named_session_confirms_and_writes_as_its_new_master(taker, monkeypatch, tmp_path, capsys, registered):  # noqa: F811
    store, _, rt, _ = taker
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda *_: store.redis)
    carried = store.next_name("sw", "eng") if registered is True else "s-261007-104655"
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", carried)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "name_session", broadcast.name_session)
    broadcast.register_session("session", 4242, "/repo", "sol", name=carried)
    table = {4242: process(4242, comm="codex", argv=("codex",))}
    monkeypatch.setattr("scripts.terminate_agent.processes", lambda proc: table)
    monkeypatch.setattr("scripts.terminate_agent.agent_environ", lambda pid, keys, proc: (carried,))
    rt.live_names = lambda: {s.name for s in sessions()}
    dead = AgentRecord(store.next_name("sw", "master"), "master", "master", seat="master@sw")
    store.seats.occupy(dead.seat, dead.name, 1)
    store.put_handoff("sw", "master", DOC, dead.seat)
    row = transfers.record(store, "sw", dead, "recycle", DOC, 1)
    transfers.attach(store, "sw", dead)
    transfers.failed(store, "sw", dead)
    if registered == "alias":
        store.names.alias(carried, store.next_name("sw", "master"))

    assert run("sw", "take-master") == 0
    name = "master@a1b2c3-0002"
    assert broadcast.session_name(4242) == name
    assert rt.live_names() == {name}
    assert os.environ["AGENTIHOOKS_AGENT_NAME"] == carried
    assert Who.from_env().name == name
    assert Who.from_env(os.environ).name == name
    assert refusal(name, Who.from_env()) == ""
    assert refusal("engineer@a1b2c3-0099", Who.from_env())
    [transfer] = [r for r in transfers.list_transfers(store, "sw") if r.get("retry_of") == row["id"]]
    assert run("sw", "confirm-handoff", transfer["id"], "--next", "Read the saved proof.") == 0
    assert transfers.list_transfers(store, "sw")[-1]["continuity"]["by"] == name

    writes = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops: writes.extend(ops) or {})
    monkeypatch.setattr(
        sys, "argv", ["ledger", "--slug", "sw", "--as", name, "comment", "phases/p1", "Master confirmed"]
    )
    ledger.main()
    assert writes[0]["by"] == name
    assert writes[0]["text"] == "Master confirmed"
    monkeypatch.setattr(ledger.repository, "token", lambda slug: "fixture")
    assert ledger.credentials("sw")["X-Ledger-Agent"] == name
    assert run("sw", "take-master") == 0
    assert [agent.name for agent in store.agents("sw")] == [name]
    capsys.readouterr()


def test_take_master_records_the_seat_the_trace_exporter_reads(taker, monkeypatch, tmp_path):  # noqa: F811
    from hooks.observability import agent_trace

    launch = {"AGENTIHOOKS_AGENT_NAME": "s-261007-104655", "AGENTIHOOKS_SWARM": "sw-doctor"}
    for key, value in launch.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "name_session", broadcast.name_session)
    broadcast.register_session("seated", 4242, "/repo", "sol", name="s-261007-104655")
    broadcast.register_session("other", 5151, "/repo", "sol", name="s-261007-104700")

    assert run("sw", "take-master") == 0
    name = broadcast.session_name(4242)
    assert name.startswith("master@")
    seated = agent_trace.identity_from_env("seated", launch)
    assert (seated.agent, seated.swarm, seated.lane, seated.task) == (name, "sw", "master", "master")
    other = agent_trace.identity_from_env("other", launch)
    assert (other.agent, other.swarm, other.lane, other.task) == ("s-261007-104655", "sw-doctor", "", "")


def _finding(store):
    kept = cli.verdict_store(store, "sw")
    kept.redis.hset(kept.key, "idle-with-claim/x", json.dumps({"measure": 1, "evidence": "seen"}))
    return kept


@pytest.fixture
def registry(taker, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "name_session", broadcast.name_session)
    return taker[0]


@pytest.mark.parametrize("launched", ["", "s-261007-104655"])
def test_a_hand_launched_master_gives_a_verdict_without_exporting_its_name(registry, capsys, monkeypatch, launched):
    if launched:
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", launched)
    broadcast.register_session("seated", 4242, "/repo", "sol", name=launched)
    assert run("sw", "take-master") == 0
    name = broadcast.session_name(4242)
    assert os.environ.get("AGENTIHOOKS_AGENT_NAME", "") == launched
    kept = _finding(registry)
    capsys.readouterr()
    assert run("sw", "verdict", "idle-with-claim/x", "resolved") == 0
    assert json.loads(kept.redis.hget(kept.key, "idle-with-claim/x"))["verdict"]["by"] == name


def test_a_named_session_that_never_took_the_seat_is_refused_a_verdict(registry, capsys):
    broadcast.register_session("unseated", 4242, "/repo", "sol", name="engineer@a1b2c3-0005")
    kept = _finding(registry)
    assert run("sw", "verdict", "idle-with-claim/x", "resolved") == 1
    assert capsys.readouterr().err == "swarm: only the master or the operator gives a finding a verdict\n"
    assert "verdict" not in json.loads(kept.redis.hget(kept.key, "idle-with-claim/x"))


def test_a_hand_launched_master_promotes_and_retires_notes_as_itself(registry):
    broadcast.register_session("seated", 4242, "/repo", "sol")
    assert run("sw", "take-master") == 0
    name = broadcast.session_name(4242)
    registry.memory.learn("eng-1@sw", "engineer@a1b2c3-0001", "lesson one because it held", 1)
    assert run("sw", "promote", "eng-1", "1", "canon", "--reason", "always held") == 0
    assert run("sw", "retire", "eng-1", "1", "--reason", "stale") == 0
    (entry,) = registry.memory.learned("eng-1@sw")
    assert (entry["promotions"][0]["by"], entry["retired"]["by"]) == (name, name)


def test_a_named_session_that_never_took_the_seat_may_not_retire_a_note(registry, capsys):
    broadcast.register_session("unseated", 4242, "/repo", "sol", name="engineer@a1b2c3-0001")
    registry.memory.learn("eng-1@sw", "engineer@a1b2c3-0001", "lesson one because it held", 1)
    put = registry.put_agent
    put("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", seat="eng-1@sw"))
    assert run("sw", "retire", "eng-1", "1", "--reason", "stale") == 1
    assert cli.ONLY_MASTER_RETIRE in capsys.readouterr().err
