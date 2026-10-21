import os

import pytest

from hooks.context import account_sessions, broadcast
from hooks.proc import Process
from scripts.gates import base
from scripts.swarm import take_master
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_ledger import ledger as remote
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api import client
from scripts.swarm_v2.runtime import process
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_take_master import taker  # noqa: F401

HIVE = "hive-ledger-credential"
HIVE_ID = "member-1"
CARRIED = "s-261013-101500"
MASTER = "master@a1b2c3-0001"
PINNED = {"X-Ledger-Token": f"minted for {MASTER}", "X-Ledger-Agent": MASTER}
MINT = {"X-Hive-Credential": HIVE, "X-Ledger-Agent": MASTER}


def _hive_client(sent):
    class HiveClient:
        def __init__(self, base, credentials, timeout=10):
            assert base == "https://hub.example"
            self.credentials = credentials

        def request(self, slug, path, payload=None):
            sent.append((slug, path, self.credentials))
            if path != "agent-token":
                return {"data": {}}
            return {"data": {"token": f"minted for {self.credentials['X-Ledger-Agent']}"}}

    return HiveClient


def _operator_token(slug):
    raise AssertionError("take-master read the operator's ledger credential")


@pytest.fixture
def remote_taker(taker, monkeypatch, tmp_path):  # noqa: F811
    for key, value in {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": "https://hub.example",
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": HIVE,
        "AGENTIHOOKS_HIVE_ID": HIVE_ID,
        "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "carried token",
        "AGENTIHOOKS_AGENT_NAME": CARRIED,
        "AGENTIHOOKS_SWARM": "",
        "AGENTIHOOKS_CONTROLLER_CREDENTIAL": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "name_session", broadcast.name_session)
    monkeypatch.setattr(process, "local_namespace", lambda: "boot/pidns")
    monkeypatch.setattr(
        take_master, "processes", lambda: {4242: Process(4242, 1, 4242, 4242, 100, "S", "codex", ("codex",))}
    )
    broadcast.register_session("session", 4242, "/repo", "sol", name=CARRIED)
    monkeypatch.setattr(remote, "BASE", "")
    monkeypatch.setattr(remote.repository, "token", _operator_token)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: taker[0].redis)
    sent = []
    monkeypatch.setattr(client, "ResourceClient", _hive_client(sent))
    return (*taker, sent)


def _admission(ledger, seen):
    def closed(slug):
        seen.append((remote.credentials(slug), authority.hive_agent(HIVE_ID, slug, MASTER)))
        return False

    ledger.closed = closed


def _masters(store):
    return [(a.name, a.hive, a.runtime_target) for a in store.agents("sw") if a.lane == "master"]


def test_take_master_on_a_remote_ledger_places_and_pins_the_new_master_before_its_first_ledger_call(
    remote_taker, monkeypatch
):
    store, ledger, _, _, sent = remote_taker
    seen = []
    _admission(ledger, seen)

    assert run("sw", "take-master") == 0

    assert seen == [(PINNED, True)]
    assert sent == [("sw", "agent-token", MINT), ("sw", "agent-token", MINT), ("sw", "metadata", PINNED)]
    assert (os.environ["AGENTIHOOKS_SWARM"], os.environ["AGENTIHOOKS_AGENT_NAME"]) == ("sw", MASTER)
    assert "AGENTIHOOKS_LEDGER_AGENT_TOKEN" not in os.environ
    assert _masters(store) == [
        (MASTER, HIVE_ID, {"host_id": HIVE_ID, "process_namespace": "boot/pidns", "pid": 4242, "pid_start": 100})
    ]
    assert store.seats.occupant("master@sw").occupant == MASTER
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    assert broadcast.session_swarm(4242) == "sw"
    who = base.Who.from_env()
    assert (who.name, who.swarm, who.pinned) == (MASTER, "sw", True)
    assert remote.credentials("sw") == PINNED


def test_a_session_already_named_this_master_mints_its_token_with_the_hive_credential(remote_taker, monkeypatch):
    store, ledger, _, _, sent = remote_taker
    store.next_name("sw", "master")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", MASTER)
    broadcast.name_session(4242, MASTER)
    seen = []
    _admission(ledger, seen)
    assert run("sw", "take-master") == 0
    assert seen == [(PINNED, True)]
    assert "AGENTIHOOKS_LEDGER_AGENT_TOKEN" not in os.environ
    assert sent == [("sw", "agent-token", MINT), ("sw", "agent-token", MINT), ("sw", "metadata", PINNED)]


def _started(store, rt):
    store.put_agent("sw", AgentRecord(store.next_name("sw", "master"), "master", "master", seat="master@sw"))
    rt.live.add(MASTER)
    assert (sorted(rt.live), _masters(store)) == ([MASTER], [(MASTER, "", {})])


def test_replace_on_a_remote_host_retires_the_old_master_before_placing_the_new_one(remote_taker, monkeypatch):
    store, ledger, rt, _, _ = remote_taker
    _started(store, rt)
    killed = []
    ledger.closed = lambda slug: killed.append(list(rt.killed)) or False
    assert run("sw", "take-master", "--replace") == 0
    assert killed == [[MASTER]]
    assert _masters(store) == [
        (
            "master@a1b2c3-0002",
            HIVE_ID,
            {"host_id": HIVE_ID, "process_namespace": "boot/pidns", "pid": 4242, "pid_start": 100},
        )
    ]


def test_a_refused_replace_on_a_remote_host_leaves_the_old_master_retired_and_no_new_one(remote_taker, monkeypatch):
    store, ledger, rt, _, _ = remote_taker
    _started(store, rt)
    ledger.closed = _unauthenticated
    assert _taken(["sw", "take-master", "--replace"]) == "unauthenticated: the ledger server refused the credential"
    assert rt.killed == [MASTER]
    assert _masters(store) == []


def test_a_remote_host_without_a_hive_id_is_refused_before_any_change(remote_taker, monkeypatch, capsys):
    store, _, _, _, sent = remote_taker
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "")
    assert run("sw", "take-master") == 1
    assert capsys.readouterr().err == (
        "swarm: take-master on a remote host needs AGENTIHOOKS_HIVE_ID from agentihooks hive add\n"
    )
    assert (_masters(store), sent, broadcast.session_name(4242)) == ([], [], CARRIED)


def test_a_workstation_on_a_hosted_ledger_needs_no_hive_id():
    assert take_master.hive_of({"SWARM_URL": "https://swarm.example"}) == ""
    assert take_master.hive_of({"SWARM_URL": "https://swarm.example", "AGENTIHOOKS_DEPLOYMENT": "local"}) == ""
    assert take_master.hive_of({"AGENTIHOOKS_DEPLOYMENT": "compose", "AGENTIHOOKS_HIVE_ID": "member-9"}) == "member-9"


def test_a_hub_address_without_a_mode_is_a_remote_host_that_needs_a_hive_id():
    hub = {"LEDGER_URL": "https://hub.example"}
    with pytest.raises(SwarmError) as refused:
        take_master.hive_of(hub)
    assert str(refused.value) == "take-master on a remote host needs AGENTIHOOKS_HIVE_ID from agentihooks hive add"
    assert take_master.hive_of({**hub, "AGENTIHOOKS_HIVE_ID": "member-9"}) == "member-9"
    assert take_master.hive_of({**hub, "AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_HIVE_ID": "member-9"}) == ""
    assert take_master.hive_of({"AGENTIHOOKS_HIVE_ID": "member-9"}) == ""


def test_a_hub_address_without_a_mode_and_no_hive_id_is_refused_before_any_change(remote_taker, monkeypatch, capsys):
    store, _, _, _, sent = remote_taker
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "")
    assert run("sw", "take-master") == 1
    assert capsys.readouterr().err == (
        "swarm: take-master on a remote host needs AGENTIHOOKS_HIVE_ID from agentihooks hive add\n"
    )
    assert (_masters(store), sent, broadcast.session_name(4242)) == ([], [], CARRIED)


def test_a_local_ledger_keeps_the_session_unpinned(taker, monkeypatch):  # noqa: F811
    store, ledger, _, _ = taker
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", HIVE_ID)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", CARRIED)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "carried token")
    assert run("sw", "take-master") == 0
    assert (os.environ["AGENTIHOOKS_SWARM"], os.environ["AGENTIHOOKS_AGENT_NAME"]) == ("", CARRIED)
    assert os.environ["AGENTIHOOKS_LEDGER_AGENT_TOKEN"] == "carried token"
    assert _masters(store) == [(MASTER, "", {"process_namespace": "boot/pidns", "pid": 4242, "pid_start": 100})]


def test_only_a_remote_session_reads_its_pinned_swarm(monkeypatch, tmp_path):
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    broadcast.register_session("session", 4242, "/repo", "sol", name=MASTER)
    assert broadcast.pin_session(4242, "sw") == 1
    assert broadcast.pin_session(5151, "sw") == 0
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    assert (base.Who.from_env().name, base.Who.from_env().swarm) == (MASTER, "")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    assert (base.Who.from_env().name, base.Who.from_env().swarm) == (MASTER, "sw")
    assert base.Who.from_env({"AGENTIHOOKS_DEPLOYMENT": "compose"}).swarm == ""
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "other")
    assert base.Who.from_env().swarm == "other"
    assert broadcast.session_swarm(5151) == ""


def _refused(slug):
    raise LedgerRefused("ledger sw refused: unauthenticated")


def _unauthenticated(slug):
    raise remote.unauthenticated("the ledger server refused the credential")


def _taken(argv):
    try:
        return run(*argv)
    except SystemExit as exc:
        return str(exc)


@pytest.mark.parametrize(
    ("refusal", "outcome"),
    [(_refused, 1), (_unauthenticated, "unauthenticated: the ledger server refused the credential")],
)
def test_a_refused_take_master_saves_no_master_and_no_seat(taker, refusal, outcome):  # noqa: F811
    store, ledger, _, _ = taker
    ledger.closed = refusal
    assert _taken(["sw", "take-master"]) == outcome
    assert [a.name for a in store.agents("sw") if a.lane == "master"] == []
    assert store.seats.occupant("master@sw").occupant == ""


def test_a_refused_take_master_retires_no_live_master(taker):  # noqa: F811
    store, ledger, rt, _ = taker
    run("sw", "start")
    seated = store.seats.occupant("master@sw")
    ledger.closed = _refused
    assert run("sw", "take-master", "--replace") == 1
    assert rt.killed == []
    assert [a.name for a in store.agents("sw") if a.lane == "master"] == [MASTER]
    assert store.seats.occupant("master@sw") == seated


def test_a_refused_reopen_of_a_closed_ledger_saves_no_master_and_no_seat(taker):  # noqa: F811
    store, ledger, _, _ = taker
    ledger.is_closed = True
    ledger.reopen = lambda slug, by: _refused(slug)
    assert run("sw", "take-master") == 1
    assert [a.name for a in store.agents("sw") if a.lane == "master"] == []
    assert store.seats.occupant("master@sw").occupant == ""


def test_a_remote_refusal_ends_the_placed_master_and_saves_no_seat_alias_or_pin(remote_taker):
    store, ledger, _, _, _ = remote_taker
    placed = []

    def refuse(slug):
        placed.append(authority.hive_agent(HIVE_ID, slug, MASTER))
        _unauthenticated(slug)

    ledger.closed = refuse
    assert _taken(["sw", "take-master"]) == "unauthenticated: the ledger server refused the credential"
    assert placed == [True]
    assert _masters(store) == []
    assert authority.hive_agent(HIVE_ID, "sw", MASTER) is False
    assert [(a.name, a.state) for a in store.execution_registry.records("sw")] == [(MASTER, "ended")]
    assert store.seats.occupant("master@sw").occupant == ""
    assert store.names.aliases(MASTER) == []
    assert broadcast.session_swarm(4242) == ""


def test_a_retake_after_a_remote_refusal_replaces_the_ended_execution(remote_taker):
    store, ledger, _, _, _ = remote_taker
    ledger.closed = _unauthenticated
    assert _taken(["sw", "take-master"]) == "unauthenticated: the ledger server refused the credential"
    ledger.closed = lambda slug: False
    assert run("sw", "take-master") == 0
    assert [(a.name, a.generation) for a in store.execution_registry.records("sw")] == [(MASTER, 1), (MASTER, 2)]
    assert _masters(store) == [
        (MASTER, HIVE_ID, {"host_id": HIVE_ID, "process_namespace": "boot/pidns", "pid": 4242, "pid_start": 100})
    ]


def test_pin_on_a_remote_host_without_a_carried_token_names_the_swarm_and_agent():
    environ = {"AGENTIHOOKS_DEPLOYMENT": "compose"}
    take_master.pin("sw", MASTER, environ)
    assert environ == {"AGENTIHOOKS_DEPLOYMENT": "compose", "AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": MASTER}


def test_take_refuses_a_live_master_unless_told_to_replace(taker, monkeypatch):  # noqa: F811
    store, _, rt, _ = taker
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    _started(store, rt)
    with pytest.raises(SwarmError) as refused:
        take_master.take(store, "sw", CARRIED, rt, 1234)
    assert str(refused.value) == f"{MASTER} is the live master of sw; run take-master --replace to retire it first"


def test_a_finished_master_still_named_live_does_not_block_take_master(taker, monkeypatch):  # noqa: F811
    store, _, rt, _ = taker
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    store.put_agent(
        "sw", AgentRecord(store.next_name("sw", "master"), "master", "master", seat="master@sw", state="finished")
    )
    rt.live.add(MASTER)
    assert run("sw", "take-master") == 0
    assert _masters(store) == [
        ("master@a1b2c3-0002", "", {"process_namespace": "boot/pidns", "pid": 4242, "pid_start": 100})
    ]


def test_take_names_a_new_master_at_the_time_it_was_taken(taker, monkeypatch):  # noqa: F811
    store, _, rt, _ = taker
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    record, _ = take_master.take(store, "sw", CARRIED, rt, 1234)
    assert (record.name, store.names.entry(MASTER)["spawned_at"]) == (MASTER, 1234)
