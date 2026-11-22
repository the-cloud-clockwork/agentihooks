"""Expert lineage records and the expert verb: starting, listing and stopping a lineage and reading its mission."""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from scripts.handoff.resolve import Resolver
from scripts.swarm import cli, expert_lineage, expert_mission
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

MASTER, ENGINEER, STRANGER = "master@a1b2c3-0001", "engineer@a1b2c3-0002", "master@d4e5f6-0001"
EXPERT, SECOND_EXPERT = "expert@a1b2c3-0001", "expert@a1b2c3-0002"
DIGEST = hashlib.sha256(GOOD.encode()).hexdigest()
ADDRESS = f"mission:sw/{DIGEST}"
TITLE = "Keep an ordered batch built"
CONTROLS = "only the operator or the master of swarm sw uses its swarm controls"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RedisStore:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: "")
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(name, raising=False)
    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", state="running", max_eng=1, max_ci=0))
    found.create(SwarmConfig("other", "/repo", 1, 0))
    found.put_agent("sw", AgentRecord(MASTER, "master", "master", seat="master@sw"))
    found.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1"))
    found.put_agent("other", AgentRecord(STRANGER, "master", "master", seat="master@other"))
    monkeypatch.setattr(cli, "connect", lambda: found)
    return found


@pytest.fixture
def mission_file(tmp_path: Path) -> Path:
    path = tmp_path / "missions" / "batch.md"
    path.parent.mkdir()
    path.write_text(GOOD, encoding="utf-8")
    return path


def acting(monkeypatch: pytest.MonkeyPatch, name: str, swarm: str = "sw") -> None:
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)


def started(store: RedisStore, effort: str = "xhigh", by: str = MASTER, at: int = 1_000) -> expert_lineage.Lineage:
    return expert_lineage.start(store, "sw", GOOD, "/missions/batch.md", effort, by, at)


def seated(store: RedisStore, name: str, lineage: str, state: str = "working") -> None:
    store.put_agent("sw", AgentRecord(name, "expert", lineage, seat=f"{lineage}@sw", state=state))


def run(*argv: str) -> int:
    return cli.main(list(argv))


def stored(store: RedisStore, part: str) -> dict[str, str]:
    found: dict[str, str] = store.redis.hgetall(store.key("sw", part))
    return found


@pytest.mark.feature("expert-mission-and-lineage")
def test_start_saves_the_mission_and_records_a_running_lineage(store: RedisStore) -> None:
    lineage = started(store)
    assert lineage == expert_lineage.Lineage(
        id="expert-1",
        seat="expert-1@sw",
        mission=ADDRESS,
        title=TITLE,
        source="/missions/batch.md",
        effort="xhigh",
        state="running",
        started_by=MASTER,
        started_at=1_000,
    )
    assert (lineage.ended_by, lineage.ended_at, lineage.note) == ("", 0, "")
    assert expert_lineage.get(store, "sw", "expert-1") == lineage
    assert expert_lineage.mission(store, "sw", lineage) == GOOD
    assert stored(store, "expert-missions") == {DIGEST: GOOD}
    assert {name: json.loads(raw) for name, raw in stored(store, "expert-lineages").items()} == {
        "expert-1": asdict(lineage)
    }
    assert store.redis.hgetall(store.key("other", "expert-lineages")) == {}


@pytest.mark.feature("expert-mission-and-lineage")
def test_each_start_takes_the_next_lineage_and_the_same_text_keeps_one_address(store: RedisStore) -> None:
    ids = [started(store, at=5).id for _ in range(11)]
    assert ids == [f"expert-{number}" for number in range(1, 12)]
    assert [row.id for row in expert_lineage.listed(store, "sw")] == ids
    assert {row.mission for row in expert_lineage.listed(store, "sw")} == {ADDRESS}
    assert stored(store, "expert-missions") == {DIGEST: GOOD}
    assert expert_lineage.start(store, "other", GOOD, "", "max", "operator", 1).id == "expert-1"


def test_lineages_list_in_the_order_started_whatever_their_ids(store: RedisStore) -> None:
    late, early = started(store, at=9), started(store, at=3)
    assert [row.id for row in expert_lineage.listed(store, "sw")] == [early.id, late.id] == ["expert-2", "expert-1"]
    assert expert_lineage.listed(store, "other") == []


def test_a_malformed_mission_is_refused_with_every_fault_and_takes_no_lineage(store: RedisStore) -> None:
    text = GOOD.replace("# Mission: ", "# ").replace("## Evidence\n", "")
    with pytest.raises(SwarmError) as refused:
        expert_lineage.start(store, "sw", text, "", "xhigh", MASTER, 1)
    assert str(refused.value) == (
        "mission refused; fix the document and start again:\n"
        "- the first line must be # Mission: followed by the mission's title\n"
        "- ## Evidence must appear once and appears 0 times"
    )
    assert stored(store, "expert-missions") == stored(store, "expert-lineages") == {}
    assert started(store).id == "expert-1"


@pytest.mark.parametrize("effort", ["high", "medium", "", "XHIGH", "maximum"])
def test_an_effort_outside_xhigh_and_max_is_refused_and_takes_no_lineage(store: RedisStore, effort: str) -> None:
    with pytest.raises(SwarmError) as refused:
        started(store, effort=effort)
    assert str(refused.value) == f"an expert runs at effort xhigh or max, not {effort}"
    assert stored(store, "expert-missions") == stored(store, "expert-lineages") == {}
    assert started(store, effort="max").id == "expert-1"


def test_an_unknown_lineage_is_refused_by_name(store: RedisStore) -> None:
    started(store)
    for call in (
        lambda: expert_lineage.get(store, "sw", "expert-9"),
        lambda: expert_lineage.get(store, "other", "expert-1"),
        lambda: expert_lineage.stop(store, "sw", "expert-9", MASTER, "gone", 2),
    ):
        with pytest.raises(SwarmError) as refused:
            call()
        assert str(refused.value) in (
            "swarm sw has no expert lineage expert-9",
            "swarm other has no expert lineage expert-1",
        )


@pytest.mark.feature("expert-mission-and-lineage")
def test_stop_ends_a_running_lineage_with_its_note_and_only_once(store: RedisStore) -> None:
    lineage = started(store)
    ended = expert_lineage.stop(store, "sw", "expert-1", "operator", "the order changed", 2_000)
    assert ended == replace(lineage, state="stopped", ended_by="operator", ended_at=2_000, note="the order changed")
    assert expert_lineage.get(store, "sw", "expert-1") == ended
    with pytest.raises(SwarmError) as refused:
        expert_lineage.stop(store, "sw", "expert-1", MASTER, "again", 3_000)
    assert str(refused.value) == "expert lineage expert-1 is stopped, not running"
    assert expert_lineage.get(store, "sw", "expert-1") == ended


@pytest.mark.parametrize("note", ["", "   ", "\n\t"])
def test_a_stop_without_a_note_is_refused_and_leaves_the_lineage_running(store: RedisStore, note: str) -> None:
    lineage = started(store)
    with pytest.raises(SwarmError) as refused:
        expert_lineage.stop(store, "sw", "expert-1", MASTER, note, 2)
    assert str(refused.value) == "stopping a lineage needs a note that says why"
    assert expert_lineage.get(store, "sw", "expert-1") == lineage


def test_a_lineage_that_ends_while_a_stop_is_written_is_not_ended_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    server = fakeredis.FakeServer()
    store, rival = (RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True)) for _ in range(2))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    started(store)
    real = expert_lineage.replace
    raced: list[str] = []

    def racing(found: Any, **changes: Any) -> Any:
        if not raced:
            raced.append("rival")
            expert_lineage.stop(rival, "sw", "expert-1", "operator", "rival got there first", 7)
        return real(found, **changes)

    monkeypatch.setattr(expert_lineage, "replace", racing)
    with pytest.raises(SwarmError) as refused:
        expert_lineage.stop(store, "sw", "expert-1", MASTER, "mine", 8)
    assert str(refused.value) == "expert lineage expert-1 is stopped, not running"
    kept = expert_lineage.get(store, "sw", "expert-1")
    assert (kept.ended_by, kept.ended_at, kept.note) == ("operator", 7, "rival got there first")


def test_a_saved_mission_that_is_gone_or_changed_is_refused(store: RedisStore) -> None:
    lineage = started(store)
    missing = f"the saved mission of lineage expert-1 is missing or does not match {ADDRESS}"
    store.redis.hset(store.key("sw", "expert-missions"), DIGEST, GOOD + "one more line\n")
    with pytest.raises(SwarmError) as changed:
        expert_lineage.mission(store, "sw", lineage)
    store.redis.hdel(store.key("sw", "expert-missions"), DIGEST)
    with pytest.raises(SwarmError) as gone:
        expert_lineage.mission(store, "sw", lineage)
    assert str(changed.value) == str(gone.value) == missing


@pytest.mark.feature("expert-mission-and-lineage")
def test_a_mission_address_resolves_while_the_swarm_holds_that_text(store: RedisStore) -> None:
    resolves = Resolver("sw", store.redis)
    assert not resolves(ADDRESS)
    assert not expert_lineage.saved(store, "sw", DIGEST)
    started(store)
    assert resolves(ADDRESS)
    assert expert_lineage.saved(store, "sw", DIGEST)
    assert not resolves(f"mission:other/{DIGEST}")
    assert not resolves(f"mission:sw/{'0' * 64}")
    assert not resolves("mission:sw")
    assert not Resolver("sw", None)(ADDRESS)


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
    monkeypatch.setattr(cli.expert_commands, "now_ms", lambda: 12_500)
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
    }
    assert expert_lineage.mission(store, "sw", expert_lineage.get(store, "sw", "expert-1")) == GOOD


def test_start_takes_the_effort_the_operator_names_and_refuses_another(
    store: RedisStore, mission_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("sw", "expert", "start", str(mission_file), "--effort", "max") == 0
    assert json.loads(capsys.readouterr().out)["effort"] == "max"
    assert run("sw", "expert", "start", str(mission_file), "--effort", "high") == 1
    assert capsys.readouterr().err == "swarm: an expert runs at effort xhigh or max, not high\n"
    assert [row.id for row in expert_lineage.listed(store, "sw")] == ["expert-1"]


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
    assert expert_lineage.listed(store, "sw") == [lineage]


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
    assert expert_lineage.listed(store, "sw") == []


@pytest.mark.feature("expert-mission-and-lineage")
def test_status_lists_each_lineage_with_its_state_effort_expert_and_title(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out == "no expert lineage\n"
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
        f"expert-3 stopped at xhigh, no expert yet: {TITLE} (started by {MASTER}, ended by {ENGINEER}: superseded)",
    ]
    assert run("sw", "expert", "status", "expert-2") == 0
    assert capsys.readouterr().out == f"expert-2 running at max, no expert yet: {TITLE} (started by operator)\n"
    assert run("sw", "expert", "status", "expert-7") == 1
    assert capsys.readouterr().err == "swarm: swarm sw has no expert lineage expert-7\n"


def test_status_as_json_carries_each_record_and_its_occupant(
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


def test_the_master_stops_a_lineage_with_a_note(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lineage = started(store)
    acting(monkeypatch, MASTER)
    monkeypatch.setattr(cli.expert_commands, "now_ms", lambda: 20_000)
    assert run("sw", "expert", "stop", "expert-1", "--note", "the operator withdrew the order") == 0
    ended = replace(lineage, state="stopped", ended_by=MASTER, ended_at=20_000, note="the operator withdrew the order")
    assert json.loads(capsys.readouterr().out) == asdict(ended)
    assert expert_lineage.get(store, "sw", "expert-1") == ended
    assert run("sw", "expert", "stop", "expert-1", "--note", "again") == 1
    assert capsys.readouterr().err == "swarm: expert lineage expert-1 is stopped, not running\n"


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
    assert {action: found.handler.__name__ for action, found in parsed.items()} == {
        action: f"cmd_{action}" for action in parsed
    }
    assert (parsed["start"].mission, parsed["start"].effort) == ("mission.md", "max")
    assert parser.parse_args(["sw", "expert", "start", "mission.md"]).effort == "xhigh"
    assert (parsed["status"].lineage, parsed["status"].json) == ("expert-2", True)
    assert (parsed["stop"].lineage, parsed["stop"].note) == ("expert-2", "why")
    assert (parser.parse_args(["sw", "expert", "status"]).lineage, parsed["mission"].lineage) == ("", "expert-2")
    for incomplete in (["sw", "expert", "stop", "expert-2"], ["sw", "expert"], ["sw", "expert", "start"]):
        with pytest.raises(SystemExit):
            parser.parse_args(incomplete)
