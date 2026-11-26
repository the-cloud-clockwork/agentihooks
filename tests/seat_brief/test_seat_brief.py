"""agentihooks seat brief: what a swarm seat, a quick seat and a standalone session read before their first command."""

from dataclasses import replace
from pathlib import Path

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import scripts.seat_brief as seat_brief
from hooks.context import injection_trace
from scripts import cli_delegates
from scripts.handoff import transfers
from scripts.quick import member, paths, registry, seeds
from scripts.seat_brief import sources
from scripts.swarm import prompt
from scripts.swarm import store as swarm_store
from scripts.swarm.store import AgentRecord, RedisStore, Unreachable

pytestmark = pytest.mark.unit

NAME = "engineer@323133-0001"
SEAT = "eng-1@sw"
SEATED = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": NAME}
HANDOFF = """# Handoff v2

## Intent
Ship the seat brief.

## Done
- The reader is written. Evidence: run 1.

## Stopped at
Waiting on checks.

## Decisions and promises
None.

## Next
1. Queue the pull request.

## Read first
None.

<!-- handoff complete -->
"""


@pytest.fixture(autouse=True)
def plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENTIHOOKS_GATE_QUARANTINE", member.DIR_ENV, member.SEAT_ENV, "HERDR_PANE_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.put_agent("sw", AgentRecord(NAME, "eng", "t1", seat=SEAT))
    monkeypatch.setattr(sources, "connect", lambda: found)
    return found


def _part(out: str, label: str) -> str:
    following = {"Handoff": "Culture", "Culture": "Latest recap", "Latest recap": "Learned notes"}
    text = out.split(f"\n{label}:\n", 1)[1]
    return text.split(f"\n{following[label]}:\n", 1)[0] if label in following else text.rstrip("\n")


def _quick(folder: Path) -> dict[str, str]:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master"),
        registry.Seat(2, "eng-1", "w1:p2", "codex", "quick-abc-eng-1"),
    )
    registry.save(folder, registry.Swarm("quick-abc", "/work", seats))
    paths.create(folder, [seat.seat for seat in seats])
    return {member.DIR_ENV: str(folder), member.SEAT_ENV: "eng-1"}


@pytest.mark.feature("seat-brief")
def test_a_swarm_seat_brief_prints_its_handoff_culture_latest_recap_and_learned_notes(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.put_handoff("sw", "t1", HANDOFF, seat=SEAT)
    store.culture.set("sw", "Merge on green.\n")
    store.memory.add_recap(SEAT, "earlier", "t0", "older recap", at=1)
    store.memory.add_recap(SEAT, "earlier", "t0", "newest recap", at=2)
    store.memory.learn(SEAT, "earlier", "own note", 1, "note")
    store.memory.learn(SEAT, "earlier", "own figure", 1, "data")
    store.memory.learn("ci-1@sw", "earlier", "shared canon", 1, "canon")
    store.memory.learn("ci-1@sw", "earlier", "foreign note", 1, "note")

    assert seat_brief.main(["brief"], SEATED) == 0

    out = capsys.readouterr().out
    assert out.startswith(f"Seat {SEAT}.\nHandoff:\n")
    assert out.index("\nHandoff:\n") < out.index("\nCulture:\n") < out.index("\nLatest recap:\n")
    assert out.index("\nLatest recap:\n") < out.index("\nLearned notes:\n")
    assert _part(out, "Handoff") == HANDOFF.strip()
    assert _part(out, "Culture") == "Merge on green."
    assert _part(out, "Latest recap") == "newest recap"
    assert _part(out, "Learned notes") == (
        "- canon: shared canon\n- note: own note\n1 data entries are kept on the seat and not shown."
    )


@pytest.mark.feature("seat-brief")
def test_a_swarm_seat_with_nothing_kept_names_each_part_as_none(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert seat_brief.main(["brief"], SEATED) == 0

    assert capsys.readouterr().out == (
        f"Seat {SEAT}.\nHandoff:\nnone\nCulture:\nnone\nLatest recap:\nnone\nLearned notes:\nnone\n"
    )


@pytest.mark.feature("seat-brief")
def test_a_corrected_learned_note_stays_withheld_from_the_brief(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = "Push straight to dev because review slows the swarm down"
    store.memory.learn(SEAT, "earlier", "kept note", 1, "note")
    store.memory.learn(SEAT, "earlier", bad, 1, "note")
    source = f"learned:{SEAT}#2"
    injection_trace.record("sess-brief", "learned", source, bad, {"seat": SEAT, "note": 2})
    injection_trace.correct("sess-brief", source, "/repos/agentihooks", "dev is protected")

    assert seat_brief.main(["brief"], SEATED) == 0

    assert _part(capsys.readouterr().out, "Learned notes") == "- note: kept note"


@pytest.mark.feature("seat-brief")
def test_a_quick_seat_brief_prints_its_handoff_culture_derived_recap_and_learned_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _quick(tmp_path)
    paths.handoff(tmp_path, "eng-1").write_text(HANDOFF)
    paths.culture(tmp_path).write_text("Ship small.\n")
    paths.learned(tmp_path, "eng-1").write_text("- Read the room first because seats post there.\n")

    assert seat_brief.main(["brief"], environ) == 0

    out = capsys.readouterr().out
    assert out.startswith("Seat eng-1 of quick swarm quick-abc.\nHandoff:\n")
    assert _part(out, "Handoff") == HANDOFF.strip()
    assert _part(out, "Culture") == "Ship small."
    assert _part(out, "Latest recap") == (
        "## Done\n- The reader is written. Evidence: run 1.\n\n## Stopped at\nWaiting on checks.\n\n"
        "## Next\n1. Queue the pull request."
    )
    assert _part(out, "Learned notes") == "- Read the room first because seats post there."


@pytest.mark.feature("seat-brief")
def test_a_quick_seat_with_empty_files_names_each_part_as_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert seat_brief.main(["brief"], _quick(tmp_path)) == 0

    assert capsys.readouterr().out == (
        "Seat eng-1 of quick swarm quick-abc.\nHandoff:\nnone\nCulture:\nnone\nLatest recap:\nnone\n"
        "Learned notes:\nnone\n"
    )


@pytest.mark.feature("seat-brief")
def test_a_successor_reads_the_handoff_its_transfer_kept_after_the_live_key_is_cleared(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    predecessor = AgentRecord("engineer@323133-0000", "eng", "t1", seat=SEAT)
    store.put_handoff("sw", "t1", HANDOFF, seat=SEAT)
    transfers.record(store, "sw", predecessor, "context", HANDOFF, 1)
    store.clear_handoff("sw", "t1")

    assert seat_brief.main(["brief"], SEATED) == 0

    assert _part(capsys.readouterr().out, "Handoff") == HANDOFF.strip()


@pytest.mark.feature("seat-brief")
def test_an_unreadable_quick_seat_file_prints_the_error_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _quick(tmp_path)
    paths.learned(tmp_path, "eng-1").unlink()
    paths.learned(tmp_path, "eng-1").mkdir()

    assert seat_brief.main(["brief"], environ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("seat brief: [Errno 21] Is a directory")


@pytest.mark.feature("seat-brief")
def test_a_missing_quick_seat_file_reads_as_none(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    environ = _quick(tmp_path)
    paths.culture(tmp_path).unlink()

    assert seat_brief.main(["brief"], environ) == 0

    assert _part(capsys.readouterr().out, "Culture") == "none"


@pytest.mark.feature("seat-brief")
def test_a_recap_keeps_only_the_sections_a_handoff_filled() -> None:
    assert sources.recap_of("## Done\nMerged.\n\n## Next\n") == "## Done\nMerged."
    assert sources.recap_of("no headings at all") == ""


@pytest.mark.feature("seat-brief")
def test_a_standalone_session_gets_the_no_seat_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert seat_brief.main(["brief"], {}) == 0

    assert capsys.readouterr().out == (
        "seat brief: this session holds no swarm or quick swarm seat, so there are no seat notes to read.\n"
    )


@pytest.mark.feature("seat-brief")
def test_a_swarm_name_without_an_agent_record_or_a_seat_holds_no_seat(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        seat_brief.main(["brief"], {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "engineer@323133-0002"}) == 0
    )
    store.put_agent("sw", replace(store.agents("sw")[0], seat=""))
    assert seat_brief.main(["brief"], SEATED) == 0

    assert capsys.readouterr().out == f"{seat_brief.NO_SEAT}\n{seat_brief.NO_SEAT}\n"


@pytest.mark.feature("seat-brief")
def test_a_store_failure_prints_the_error_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down() -> RedisStore:
        raise Unreachable("Redis is unreachable (refused); the swarm refuses to run without it")

    monkeypatch.setattr(sources, "connect", down)

    assert seat_brief.main(["brief"], SEATED) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "seat brief: Redis is unreachable (refused); the swarm refuses to run without it\n"


@pytest.mark.feature("seat-brief")
def test_a_redis_error_while_reading_prints_the_error_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused() -> RedisStore:
        raise RedisConnectionError("Connection refused")

    monkeypatch.setattr(sources, "connect", refused)

    assert seat_brief.main(["brief"], SEATED) == 1

    assert capsys.readouterr().err == "seat brief: Connection refused\n"


@pytest.mark.feature("seat-brief")
def test_a_quick_seat_is_read_before_a_swarm_seat_the_session_also_names(
    store: RedisStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert seat_brief.main(["brief"], {**SEATED, **_quick(tmp_path)}) == 0

    assert capsys.readouterr().out.startswith("Seat eng-1 of quick swarm quick-abc.\n")


@pytest.mark.feature("seat-brief")
def test_a_swarm_name_outside_every_lane_holds_no_seat(store: RedisStore, capsys: pytest.CaptureFixture[str]) -> None:
    store.put_agent("sw", AgentRecord("someone", "eng", "t2", seat="eng-2@sw"))

    assert seat_brief.main(["brief"], {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "someone"}) == 0

    assert capsys.readouterr().out == f"{seat_brief.NO_SEAT}\n"


@pytest.mark.feature("seat-brief")
def test_a_swarm_seat_reads_the_swarm_store_the_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(swarm_store, "connect", lambda: found)

    source = sources.find(SEATED)

    assert source == sources.RedisSeat("sw", NAME, sources.connect)
    assert sources.connect() is found


@pytest.mark.feature("seat-brief")
def test_the_seat_command_runs_through_the_agentihooks_command_line() -> None:
    assert cli_delegates.delegated_cli(["seat", "brief"]) is seat_brief.main
    assert "handoff, culture, latest recap and learned notes" in seat_brief.build_parser().format_help()
    with pytest.raises(SystemExit):
        seat_brief.main([], {})


@pytest.mark.feature("seat-brief")
def test_priming_lines_end_with_the_seat_brief_command_with_or_without_history() -> None:
    recaps = [{"occupant": "a", "task": "t", "text": "newest"}, {"occupant": "b", "task": "t", "text": "older"}]
    for task in (
        {"seat": SEAT},
        {"seat": SEAT, "culture": "Merge on green."},
        {"seat": SEAT, "culture": "Merge on green.", "recaps": recaps},
    ):
        assert prompt.priming_lines(task)[-1] == prompt.SEAT_BRIEF
    assert prompt.SEAT_BRIEF == (
        "Run agentihooks seat brief before your first command: it prints your seat's handoff, culture, latest recap "
        "and learned notes from the store that keeps them."
    )


@pytest.mark.feature("seat-brief")
def test_every_quick_opening_names_the_seat_brief_command(tmp_path: Path) -> None:
    _quick(tmp_path)
    swarm = registry.load(tmp_path)
    assert swarm is not None
    for seat in swarm.seats:
        opening = seeds.opening(member.Member(tmp_path, swarm, seat))
        assert "Run `agentihooks seat brief` before your first command" in opening
