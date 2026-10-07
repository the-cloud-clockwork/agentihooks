import os
from pathlib import Path

import pytest

from scripts.handoff import transfers
from scripts.swarm import cli, take_master
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_close import closing  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")
DOC = "# Handoff v2\n## Next\nRead the saved proof.\n## Read first\nNone\n"


@pytest.fixture
def taker(env, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    named = []
    ledger.calls, ledger.is_closed = [], False
    ledger.closed = lambda slug: ledger.is_closed
    ledger.reopen = lambda slug, by: ledger.calls.append(("reopened", by))
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.setattr(take_master, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "harness_of", lambda pid: "codex")
    monkeypatch.setattr(take_master, "name_session", lambda pid, name: named.append((pid, name)) or 1)
    run("sw", "create", "--repo", "/repo", "--max-eng-agents", "0", "--max-ci-agents", "0")
    return store, ledger, rt, named


def _masters(store):
    return [(a.name, a.seat, a.harness) for a in store.agents("sw") if a.lane == "master"]


def test_take_master_seats_this_session_and_names_it(taker, capsys):
    store, _, rt, named = taker
    assert run("sw", "take-master") == 0
    assert _masters(store) == [("master@a1b2c3-0001", "master@sw", "codex")]
    assert store.seats.occupant("master@sw").occupant == "master@a1b2c3-0001"
    assert named == [(4242, "master@a1b2c3-0001")]
    assert rt.masters == []


def test_take_master_records_the_model_and_effort_the_session_launched_with(taker, monkeypatch):
    store, _, _, _ = taker
    argv = ("codex", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"')
    monkeypatch.setattr(take_master, "harness_of", lambda pid: "codex" if pid == 4242 else "claude")
    monkeypatch.setattr(take_master, "argv_of", lambda pid: argv if pid == 4242 else ())
    assert run("sw", "take-master") == 0
    [master] = [a for a in store.agents("sw") if a.lane == "master"]
    assert (master.harness, master.model, master.effort) == ("codex", "gpt-6.1-sol", "high")
    assert master.started_at > 0


def test_take_master_records_the_mounted_profile(taker, monkeypatch):
    store, _, _, _ = taker
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "master")
    assert run("sw", "take-master") == 0
    assert next(a for a in store.agents("sw") if a.lane == "master").profile == "master"


def test_argv_of_reads_a_process_command_line():
    own = Path("/proc/self/cmdline").read_bytes().split(b"\0")
    assert take_master.argv_of(os.getpid()) == tuple(arg.decode() for arg in own if arg)
    assert take_master.argv_of(2**22 + 1) == ()


@pytest.mark.parametrize("carried", ["s-261007-104655", "master@d44a5a-0001", "engineer@a1b2c3-0004"])
def test_a_session_carrying_another_name_is_seated_under_a_minted_master_name(taker, monkeypatch, carried):
    store, _, _, named = taker
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", carried)
    assert run("sw", "take-master") == 0
    assert _masters(store) == [("master@a1b2c3-0001", "master@sw", "codex")]
    assert named == [(4242, "master@a1b2c3-0001")]
    assert store.names.resolve(carried) == "master@a1b2c3-0001"


def test_a_session_already_named_as_this_swarms_master_keeps_its_name(taker, monkeypatch):
    store, _, _, named = taker
    name = store.next_name("sw", "master")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    assert run("sw", "take-master") == 0
    assert _masters(store) == [(name, "master@sw", "codex")]
    assert named == []


def test_a_master_retaking_its_own_seat_keeps_its_record_and_name(taker, monkeypatch):
    store, _, rt, named = taker
    name = store.next_name("sw", "master")
    store.put_agent("sw", AgentRecord(name, "master", "master", seat="master@sw"))
    rt.live.add(name)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    assert run("sw", "take-master") == 0
    assert rt.killed == [] and named == []
    assert _masters(store) == [(name, "master@sw", "codex")]
    assert store.redis.lrange(store.key("sw", "history"), 0, -1) == []


def test_a_hand_taken_master_under_its_session_name_is_renamed_without_killing_itself(taker, monkeypatch):
    store, _, rt, named = taker
    store.put_agent("sw", AgentRecord("s-261007-104655", "master", "master", seat="master@sw"))
    rt.live.add("s-261007-104655")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "s-261007-104655")
    assert run("sw", "take-master") == 0
    assert rt.killed == []
    assert _masters(store) == [("master@a1b2c3-0001", "master@sw", "codex")]
    assert store.seats.occupant("master@sw").occupant == "master@a1b2c3-0001"


def test_take_master_refuses_while_another_master_is_live(taker, capsys):
    store, _, rt, named = taker
    run("sw", "start")
    assert run("sw", "take-master") == 1
    assert "master@a1b2c3-0001 is the live master" in capsys.readouterr().err
    assert [m[0] for m in _masters(store)] == ["master@a1b2c3-0001"]
    assert rt.killed == [] and named == []


def test_replace_retires_the_live_master_first(taker):
    store, _, rt, named = taker
    run("sw", "start")
    assert run("sw", "take-master", "--replace") == 0
    assert rt.killed == ["master@a1b2c3-0001"]
    assert _masters(store) == [("master@a1b2c3-0002", "master@sw", "codex")]
    assert store.seats.occupant("master@sw").occupant == "master@a1b2c3-0002"


def test_a_dead_master_record_is_dropped_without_replace(taker):
    store, _, rt, _ = taker
    store.put_agent("sw", AgentRecord(store.next_name("sw", "master"), "master", "master", seat="master@sw"))
    assert run("sw", "take-master") == 0
    assert [m[0] for m in _masters(store)] == ["master@a1b2c3-0002"]


def test_take_master_traces_the_priming_it_printed_into_this_session(taker, monkeypatch):
    from hooks.context import injection_trace

    store, _, _, _ = taker
    store.culture.set("sw", "- merge fast")
    store.memory.learn("master@sw", "m0", "keep the ledger current", 1)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-master")
    assert run("sw", "take-master") == 0
    rows = [(r["layer"], r["source"], r["text"]) for r in injection_trace.trace("sess-master")]
    assert rows == [
        ("culture", "culture:sw#1", "- merge fast"),
        ("learned", "learned:master@sw#1", "keep the ledger current"),
    ]


def test_take_master_prints_the_swarm_repo_peer_and_autonomy(taker, capsys):
    store, _, _, _ = taker
    store.set_peer("sw", "other-swarm")
    run("sw", "set", "autonomy=full")
    capsys.readouterr()
    assert run("sw", "take-master") == 0
    out = capsys.readouterr().out
    assert "working over the repo /repo" in out
    assert "master@other-swarm" in out
    assert "This swarm runs at full autonomy" in out


def test_take_master_outside_a_claude_session_traces_nothing(taker, monkeypatch):
    from hooks import config

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    taker[0].culture.set("sw", "- merge fast")
    assert run("sw", "take-master") == 0
    assert not (config.AGENTIHOOKS_HOME / "injections").exists()


def test_take_master_reopens_a_closed_ledger_and_runs_a_stopped_swarm(taker):
    store, ledger, _, _ = taker
    store.update("sw", state="stopped")
    ledger.is_closed = True
    assert run("sw", "take-master") == 0
    assert ledger.calls == [("reopened", "master@a1b2c3-0001")]
    assert store.config("sw").state == "running"


def test_an_open_ledger_and_a_paused_swarm_stay_as_they_are(taker):
    store, ledger, _, _ = taker
    assert run("sw", "take-master") == 0
    assert ledger.calls == []
    assert store.config("sw").state == "paused"


def test_take_master_prints_the_full_master_priming(taker, capsys):
    store, _, _, _ = taker
    store.culture.set("sw", "Keep the record straight")
    store.memory.learn("master@sw", "master@a1b2c3-0000", "Answer the operator first", 1)
    store.memory.add_recap("master@sw", "master@a1b2c3-0000", "master", "Left the docs task waiting on review", 1)
    store.put_handoff("sw", "master", "Continue from the master handoff notes", "master@sw")
    capsys.readouterr()
    assert run("sw", "take-master") == 0
    out = capsys.readouterr().out
    assert out.startswith("You are master@a1b2c3-0001, the master of swarm sw")
    for part in (
        "Keep the record straight",
        "Answer the operator first",
        "Left the docs task waiting on review",
        "Continue from the master handoff notes",
        "Your standing duties:",
        "agentihooks swarm sw --as master@a1b2c3-0001 say --to operator",
    ):
        assert part in out, part
    assert store.handoff("sw", "master") == ""


def test_the_tick_sees_the_taken_master_and_spawns_no_second_one(taker):
    store, ledger, rt, _ = taker
    store.update("sw", state="running")
    run("sw", "take-master")
    rt.live.add("master@a1b2c3-0001")
    for at in (1, 10**12):
        tick("sw", store, ledger, rt, at)
    assert rt.masters == []
    assert [m[0] for m in _masters(store)] == ["master@a1b2c3-0001"]


def test_close_retires_the_taken_master(closing, monkeypatch):  # noqa: F811
    store, ledger, rt, _ = closing
    ledger.closed = lambda slug: False
    monkeypatch.setattr(take_master, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "harness_of", lambda pid: "claude")
    monkeypatch.setattr(take_master, "name_session", lambda pid, name: 1)
    assert run("sw", "take-master", "--replace") == 0
    rt.live.add("master@a1b2c3-0002")
    assert run("sw", "close", "--now") == 0
    assert "master@a1b2c3-0002" in rt.killed
    assert store.agents("sw") == []


def test_an_unregistered_session_without_a_name_is_refused(taker, monkeypatch, capsys):
    store, _, _, _ = taker
    monkeypatch.setattr(take_master, "name_session", lambda pid, name: 0)
    assert run("sw", "take-master") == 1
    assert "not registered" in capsys.readouterr().err
    assert _masters(store) == []


def test_the_open_master_transfer_moves_to_the_new_occupant(taker, monkeypatch, capsys):
    store, _, rt, _ = taker
    dead = AgentRecord(store.next_name("sw", "master"), "master", "master", seat="master@sw")
    store.seats.occupy(dead.seat, dead.name, 1)
    store.put_handoff("sw", "master", DOC, "master@sw")
    row = transfers.record(store, "sw", dead, "recycle", DOC, 1)
    transfers.attach(store, "sw", dead)
    transfers.failed(store, "sw", dead)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "s-261007-104655")
    capsys.readouterr()
    assert run("sw", "take-master") == 0
    [open_row] = [r for r in transfers.list_transfers(store, "sw") if r.get("retry_of") == row["id"]]
    assert open_row["successor"] == "master@a1b2c3-0002"
    assert f"confirm-handoff {open_row['id']} --next" in capsys.readouterr().out
    rt.live.add("master@a1b2c3-0002")
    [master] = [a for a in store.agents("sw") if a.lane == "master"]
    confirmed = transfers.confirm(store, "sw", open_row["id"], master, "Read the saved proof.", 2)
    assert confirmed["continuity"]["by"] == "master@a1b2c3-0002"


def test_cli_parses_take_master():
    args = cli.build_parser().parse_args(["sw", "take-master", "--replace"])
    assert (args.command, args.replace) == ("take-master", True)
