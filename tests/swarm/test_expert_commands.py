"""The expert verb of the swarm command: the mission template, starting and stopping a lineage, where the running
lineages stand and the mission an expert reads."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, expert_commands, expert_lineage, expert_mission
from scripts.swarm.store import AgentRecord, RedisStore
from tests.swarm.expert_fixture import (
    ADDRESS,
    CONTROLS,
    ENGINEER,
    EXPERT,
    MASTER,
    SECOND_EXPERT,
    STRANGER,
    TITLE,
    acting,
    mission_file,
    run,
    seated,
    started,
    store,
)
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit
_fixtures = mission_file, store


def test_the_template_command_prints_the_mission_template(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("sw", "expert", "template") == 0
    assert capsys.readouterr().out == expert_mission.TEMPLATE


@pytest.mark.feature("expert-mission-and-lineage")
@pytest.mark.parametrize("actor,by", [("", "operator"), (MASTER, MASTER)])
def test_the_operator_and_the_master_start_a_lineage_from_a_mission_file(
    store: RedisStore,
    mission_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actor: str,
    by: str,
) -> None:
    if actor:
        acting(monkeypatch, actor)
    monkeypatch.setattr(expert_commands, "now_ms", lambda: 12_500)
    assert run("sw", "expert", "start", str(mission_file)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "id": "expert-1",
        "seat": "expert-1@sw",
        "mission": ADDRESS,
        "title": TITLE,
        "source": str(mission_file.resolve()),
        "effort": "xhigh",
        "state": "running",
        "started_by": by,
        "started_at": 12_500,
        "ended_by": "",
        "ended_at": 0,
        "note": "",
        "launch_failure": {},
    }
    assert expert_lineage.mission(store, "sw", expert_lineage.get(store, "sw", "expert-1")) == GOOD
    assert store.handoff("sw", "expert-1") == "" and store.redis.hgetall(store.key("sw", "transfers")) == {}


def test_start_takes_the_effort_the_operator_names_and_refuses_another(
    store: RedisStore, mission_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("sw", "expert", "start", str(mission_file), "--effort", "max") == 0
    assert json.loads(capsys.readouterr().out)["effort"] == "max"
    assert run("sw", "expert", "start", str(mission_file), "--effort", "high") == 1
    assert capsys.readouterr().err == "swarm: an expert runs at effort xhigh or max, not high\n"
    assert [row.id for row in expert_lineage.running(store, "sw")] == ["expert-1"]


@pytest.mark.parametrize("actor,swarm", [(ENGINEER, "sw"), (STRANGER, "other")])
def test_nobody_but_the_master_and_the_operator_starts_or_stops_a_lineage(
    store: RedisStore,
    mission_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actor: str,
    swarm: str,
) -> None:
    lineage = started(store)
    acting(monkeypatch, actor, swarm)
    assert run("sw", "expert", "start", str(mission_file)) == 1
    assert capsys.readouterr().err == f"swarm: {CONTROLS}, and {actor} is neither\n"
    assert run("sw", "expert", "stop", "expert-1", "--note", "not mine to stop") == 1
    assert capsys.readouterr().err == f"swarm: {CONTROLS}, and {actor} is neither\n"
    assert expert_lineage.running(store, "sw") == [lineage]


def test_a_mission_file_that_cannot_be_read_or_is_malformed_is_refused(
    store: RedisStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nowhere.md"
    assert run("sw", "expert", "start", str(missing)) == 1
    assert capsys.readouterr().err == f"swarm: cannot read the mission {missing}: No such file or directory\n"
    bad = tmp_path / "bad.md"
    bad.write_text(GOOD.replace("## How to report\n", ""), encoding="utf-8")
    assert run("sw", "expert", "start", str(bad)) == 1
    assert capsys.readouterr().err == (
        "swarm: mission refused; fix the document and start again:\n"
        "- ## How to report must appear once and appears 0 times\n"
    )
    assert expert_lineage.running(store, "sw") == []


def test_a_mission_file_that_is_not_utf8_text_is_refused_by_name(
    store: RedisStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = tmp_path / "binary.md"
    binary.write_bytes(GOOD.encode() + b"\xff\xfe not text\n")
    assert run("sw", "expert", "start", str(binary)) == 1
    assert capsys.readouterr().err == f"swarm: cannot read the mission {binary}: it is not UTF-8 text\n"
    assert expert_lineage.running(store, "sw") == []


@pytest.mark.feature("expert-mission-and-lineage")
@pytest.mark.parametrize(
    "action",
    [
        ("start", "MISSION"),
        ("status",),
        ("status", "expert-1"),
        ("stop", "expert-1", "--note", "the order changed"),
        ("mission", "expert-1"),
        ("complete", "MISSION"),
        ("proof", "expert-1"),
    ],
)
def test_the_expert_verb_refuses_a_swarm_that_does_not_exist(
    store: RedisStore, mission_file: Path, capsys: pytest.CaptureFixture[str], action: tuple[str, ...]
) -> None:
    argv = [str(mission_file) if part == "MISSION" else part for part in action]
    assert run("nope", "expert", *argv) == 1
    assert capsys.readouterr().err == "swarm: no swarm nope\n"
    assert expert_lineage.running(store, "nope") == []
    assert store.redis.exists(store.key("nope", "expert-seq")) == 0


@pytest.mark.feature("expert-mission-and-lineage")
def test_status_lists_each_running_lineage_and_shows_an_ended_one_by_name(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out == "no running expert lineage\n"
    started(store, at=1)
    started(store, effort="max", by="operator", at=2)
    started(store, at=3)
    seated(store, EXPERT, "expert-1", state="finished")
    seated(store, SECOND_EXPERT, "expert-1")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "expert-2"))
    expert_lineage.stop(store, "sw", "expert-3", ENGINEER, "superseded", 4)
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out.splitlines() == [
        f"expert-1 running at xhigh, {SECOND_EXPERT}: {TITLE} (started by {MASTER})",
        f"expert-2 running at max, no expert yet: {TITLE} (started by operator)",
    ]
    assert run("sw", "expert", "status", "expert-2") == 0
    assert capsys.readouterr().out == f"expert-2 running at max, no expert yet: {TITLE} (started by operator)\n"
    assert run("sw", "expert", "status", "expert-3") == 0
    assert capsys.readouterr().out == (
        f"expert-3 stopped at xhigh: {TITLE} (started by {MASTER}, ended by {ENGINEER}: superseded)\n"
    )
    assert run("sw", "expert", "status", "expert-7") == 1
    assert capsys.readouterr().err == "swarm: swarm sw has no expert lineage expert-7\n"
    for ended in ("expert-1", "expert-2"):
        expert_lineage.stop(store, "sw", ended, MASTER, "done with it", 5)
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out == "no running expert lineage\n"


def test_status_as_json_carries_each_running_record_and_its_occupant(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = started(store, at=1), started(store, effort="max", at=2)
    seated(store, EXPERT, "expert-2")
    assert run("sw", "expert", "status", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [
        {**asdict(first), "occupant": ""},
        {**asdict(second), "occupant": EXPERT},
    ]
    assert run("sw", "expert", "status", "expert-1", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [{**asdict(first), "occupant": ""}]
    ended = expert_lineage.stop(store, "sw", "expert-1", MASTER, "superseded", 3)
    assert run("sw", "expert", "status", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [{**asdict(second), "occupant": EXPERT}]
    assert run("sw", "expert", "status", "expert-1", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [{**asdict(ended), "occupant": ""}]


@pytest.mark.feature("expert-launch-failure-reason")
def test_status_names_the_last_failed_launch_while_the_seat_is_empty_and_carries_it_as_json(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    started(store, at=1)
    expert_lineage.launch_failed(store, "sw", "expert-1", EXPERT, "herdr refused the pane: exit 3", 7)
    failure = {"at": 7, "agent": EXPERT, "reason": "herdr refused the pane: exit 3"}
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out == (
        f"expert-1 running at xhigh, no expert yet: {TITLE} "
        f"(started by {MASTER}; last launch as {EXPERT} failed: herdr refused the pane: exit 3)\n"
    )
    seated(store, EXPERT, "expert-1", state="finished")
    assert run("sw", "expert", "status", "expert-1") == 0
    assert capsys.readouterr().out.endswith(f"; last launch as {EXPERT} failed: herdr refused the pane: exit 3)\n")
    seated(store, SECOND_EXPERT, "expert-1")
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out == (f"expert-1 running at xhigh, {SECOND_EXPERT}: {TITLE} (started by {MASTER})\n")
    assert run("sw", "expert", "status", "--json") == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["launch_failure"], row["occupant"]) == (failure, SECOND_EXPERT)
    store.drop_agent("sw", SECOND_EXPERT)
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "superseded", 9)
    assert run("sw", "expert", "status", "expert-1") == 0
    assert capsys.readouterr().out == (
        f"expert-1 stopped at xhigh: {TITLE} (started by {MASTER}, ended by {MASTER}: superseded)\n"
    )


def test_the_master_stops_a_lineage_with_a_note(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lineage = started(store)
    acting(monkeypatch, MASTER)
    monkeypatch.setattr(expert_commands, "now_ms", lambda: 20_000)
    assert run("sw", "expert", "stop", "expert-1", "--note", "the operator withdrew the order") == 0
    ended = replace(lineage, state="stopped", ended_by=MASTER, ended_at=20_000, note="the operator withdrew the order")
    assert json.loads(capsys.readouterr().out) == asdict(ended)
    assert expert_lineage.get(store, "sw", "expert-1") == ended
    assert run("sw", "expert", "stop", "expert-1", "--note", "again") == 1
    assert capsys.readouterr().err == "swarm: expert lineage expert-1 is stopped, not running\n"


@pytest.mark.feature("expert-seat-recovery")
def test_stopping_a_lineage_sends_the_mail_waiting_on_its_seat_back_to_the_master(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lineage = started(store)
    inbox = InboxStore(store.redis)
    waiting = inbox.send(ENGINEER, lineage.seat, "Read the new evidence")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "stop", "expert-1", "--note", "the order changed") == 0
    capsys.readouterr()
    moved = inbox.get(waiting.id)
    assert (moved.state, moved.address, moved.reason) == (
        "pending",
        "master@sw",
        "expert lineage expert-1 is stopped and no expert holds its seat any more",
    )


@pytest.mark.feature("expert-mission-and-lineage")
def test_an_expert_reads_the_mission_of_its_own_lineage_and_no_other(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started(store)
    started(store)
    seated(store, EXPERT, "expert-2")
    acting(monkeypatch, EXPERT)
    printed = f"{ADDRESS} {TITLE}\n\n{GOOD}"
    assert run("sw", "expert", "mission") == 0
    assert capsys.readouterr().out == printed
    assert run("sw", "expert", "mission", "expert-2") == 0
    assert capsys.readouterr().out == printed
    assert run("sw", "expert", "mission", "expert-1") == 1
    assert capsys.readouterr().err == f"swarm: {EXPERT} works lineage expert-2 and reads the mission of no other\n"


@pytest.mark.parametrize(
    "ending,printed",
    [
        ("Answer to MAX\n\n\n", "merges.\nAnswer to MAX\n"),
        ("Answer in the pane.  \n", "merges.\nAnswer in the pane.  \n"),
    ],
)
def test_a_mission_prints_as_saved_and_loses_only_the_line_ends_that_close_it(
    store: RedisStore, capsys: pytest.CaptureFixture[str], ending: str, printed: str
) -> None:
    lineage = expert_lineage.start(store, "sw", GOOD + ending, "/missions/batch.md", "xhigh", MASTER, 1_000)
    seated(store, EXPERT, lineage.id)
    assert run("sw", "--as", EXPERT, "expert", "mission") == 0
    assert capsys.readouterr().out.endswith(printed)


def test_the_master_and_the_operator_read_a_named_mission_and_a_worker_none(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started(store)
    seated(store, EXPERT, "expert-1")
    printed = f"{ADDRESS} {TITLE}\n\n{GOOD}"
    assert run("sw", "--as", EXPERT, "expert", "mission") == 0
    assert capsys.readouterr().out == printed
    assert run("sw", "expert", "mission", "expert-1") == 0
    assert capsys.readouterr().out == printed
    assert run("sw", "expert", "mission") == 1
    assert capsys.readouterr().err == (
        "swarm: name the lineage whose mission to print; only an expert has one of its own\n"
    )
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "mission", "expert-1") == 0
    assert capsys.readouterr().out == printed
    acting(monkeypatch, ENGINEER)
    assert run("sw", "expert", "mission", "expert-1") == 1
    assert capsys.readouterr().err == f"swarm: {CONTROLS}, and {ENGINEER} is neither\n"


def test_the_expert_verb_parses_each_action_to_its_handler() -> None:
    parser = cli.build_parser()
    parsed = {
        action: parser.parse_args(["sw", "expert", action, *rest])
        for action, rest in {
            "template": [],
            "start": ["mission.md", "--effort", "max"],
            "status": ["expert-2", "--json"],
            "stop": ["expert-2", "--note", "why"],
            "mission": ["expert-2"],
        }.items()
    }
    assert {action: (found.command, found.action) for action, found in parsed.items()} == {
        action: ("expert", action) for action in parsed
    }
    assert {action: found.handler for action, found in parsed.items()} == {
        "template": expert_commands.cmd_template,
        "start": expert_commands.cmd_start,
        "status": expert_commands.cmd_status,
        "stop": expert_commands.cmd_stop,
        "mission": expert_commands.cmd_mission,
    }
    assert (parsed["start"].mission, parsed["start"].effort) == ("mission.md", "max")
    assert parser.parse_args(["sw", "expert", "start", "mission.md"]).effort == "xhigh"
    assert (parsed["start"].handoff, parsed["start"].source) == ("", "")
    assert (parsed["status"].lineage, parsed["status"].json) == ("expert-2", True)
    assert (parsed["stop"].lineage, parsed["stop"].note) == ("expert-2", "why")
    assert (parser.parse_args(["sw", "expert", "status"]).lineage, parsed["mission"].lineage) == ("", "expert-2")
    for incomplete in (["sw", "expert", "stop", "expert-2"], ["sw", "expert"], ["sw", "expert", "start"]):
        with pytest.raises(SystemExit):
            parser.parse_args(incomplete)
