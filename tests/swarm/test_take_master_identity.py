import os
import sys

import pytest

from hooks.context import account_sessions, broadcast
from scripts.gates.base import Who
from scripts.gates.identity import refusal
from scripts.handoff import transfers
from scripts.swarm import take_master
from scripts.swarm.store import AgentRecord
from scripts.swarm_ledger import ledger
from scripts.terminate_agent import sessions
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_take_master import DOC, taker  # noqa: F401
from tests.test_terminate_agent import process

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.mark.parametrize("registered", [False, True, "alias"])
def test_a_named_session_confirms_and_writes_as_its_new_master(taker, monkeypatch, tmp_path, capsys, registered):  # noqa: F811
    store, _, rt, _ = taker
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
    monkeypatch.setattr(ledger.repository, "read_page", lambda slug: {})
    monkeypatch.setattr(ledger.core, "read_token", lambda page: "fixture")
    assert ledger.credentials("sw")["X-Ledger-Agent"] == name
    assert run("sw", "take-master") == 0
    assert [agent.name for agent in store.agents("sw")] == [name]
    capsys.readouterr()
