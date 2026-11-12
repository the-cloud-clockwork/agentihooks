from pathlib import Path

import pytest

from scripts.quick import launch, member, paths, registry, room, tick

pytestmark = pytest.mark.unit


def _swarm(folder: Path, eng_state: str = registry.READY) -> registry.Swarm:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master"),
        registry.Seat(2, "eng-1", "w1:p2", "codex", "quick-abc-eng-1", state=eng_state),
    )
    swarm = registry.Swarm("quick-abc", "/work", seats)
    registry.save(folder, swarm)
    paths.create(folder, ["master", "eng-1"])
    return swarm


@pytest.mark.feature("quick-swarm")
def test_room_messages_reach_their_seat_once(tmp_path):
    _swarm(tmp_path)
    room.post(tmp_path, "eng-1", "master", "plan ready", 100)
    room.post(tmp_path, "master", "eng-1", "go ahead", 101)
    assert room.waiting(tmp_path, "master") == 1
    assert [line["text"] for line in room.unread(tmp_path, "master")] == ["plan ready"]
    assert room.unread(tmp_path, "master") == [] and room.waiting(tmp_path, "master") == 0
    assert room.unread(tmp_path, "eng-1") == [{"at": 101, "from": "master", "to": "eng-1", "text": "go ahead"}]


def test_an_empty_room_has_nothing_waiting(tmp_path):
    _swarm(tmp_path)
    assert room.waiting(tmp_path, "eng-1") == 0 and room.unread(tmp_path, "eng-1") == []


class FakeHerdr:
    def __init__(self, status: str = "idle", typed: str = "", bare: bool = False) -> None:
        self._status, self._typed, self._bare = status, typed, bare
        self.prompts: list[tuple[str, str]] = []

    def status(self, pane_id: str) -> str:
        return self._status

    def typed(self, pane_id: str) -> str:
        return self._typed

    def bare(self, pane_id: str) -> bool:
        return self._bare

    def prompt(self, pane_id: str, text: str) -> bool:
        self.prompts.append((pane_id, text))
        return True


def _round(folder: Path, herdr: FakeHerdr, now: float = 10_000.0) -> list[dict]:
    relaunched: list[str] = []
    outcomes = tick.round_for(folder, herdr, lambda f, seat: relaunched.append(seat) or "started", now)
    return [*outcomes, *({"relaunched": seat} for seat in relaunched)]


@pytest.mark.feature("quick-swarm")
def test_an_idle_engineer_with_open_stages_is_nudged_and_the_master_never_is(tmp_path):
    _swarm(tmp_path)
    paths.plan(tmp_path, "eng-1").write_text("- [x] one\n- [ ] two\n")
    paths.plan(tmp_path, "master").write_text("- [ ] track\n")
    herdr = FakeHerdr()
    outcomes = _round(tmp_path, herdr)
    assert herdr.prompts == [("w1:p2", tick.NUDGE)]
    assert {row["seat"]: row["outcome"] for row in outcomes} == {"master": "idle", "eng-1": "nudged"}


def test_a_nudge_waits_its_interval_before_repeating(tmp_path):
    _swarm(tmp_path)
    paths.plan(tmp_path, "eng-1").write_text("- [ ] two\n")
    herdr = FakeHerdr()
    _round(tmp_path, herdr, now=10_000.0)
    _round(tmp_path, herdr, now=10_000.0 + tick.NUDGE_EVERY_S - 1)
    _round(tmp_path, herdr, now=10_000.0 + tick.NUDGE_EVERY_S)
    assert len(herdr.prompts) == 2


@pytest.mark.parametrize(("status", "typed"), [("working", ""), ("blocked", ""), ("idle", "half a line")])
def test_a_busy_pane_or_one_holding_typed_text_is_never_typed_into(tmp_path, status, typed):
    _swarm(tmp_path)
    paths.plan(tmp_path, "eng-1").write_text("- [ ] two\n")
    room.post(tmp_path, "eng-1", "master", "hello", 1)
    herdr = FakeHerdr(status=status, typed=typed)
    _round(tmp_path, herdr)
    assert herdr.prompts == []


@pytest.mark.feature("quick-swarm")
def test_an_idle_master_is_told_once_about_new_room_lines(tmp_path):
    _swarm(tmp_path)
    room.post(tmp_path, "eng-1", "master", "plan ready", 1)
    herdr = FakeHerdr()
    _round(tmp_path, herdr)
    _round(tmp_path, herdr)
    assert herdr.prompts == [("w1:p1", tick.ROOM.format(count=1))]
    room.post(tmp_path, "eng-1", "master", "done", 2)
    _round(tmp_path, herdr)
    assert herdr.prompts[-1] == ("w1:p1", tick.ROOM.format(count=2))


@pytest.mark.feature("quick-swarm")
def test_a_pending_seat_at_a_bare_shell_is_relaunched(tmp_path):
    _swarm(tmp_path, eng_state=registry.PENDING)
    outcomes = _round(tmp_path, FakeHerdr(bare=True))
    assert {"relaunched": "eng-1"} in outcomes
    assert not any(row.get("seat") == "eng-1" and row.get("outcome") == "nudged" for row in outcomes)


def test_a_pending_seat_whose_agent_still_runs_waits(tmp_path):
    _swarm(tmp_path, eng_state=registry.PENDING)
    outcomes = _round(tmp_path, FakeHerdr(bare=False))
    assert {"seat": "eng-1", "outcome": "pending"} in outcomes and {"relaunched": "eng-1"} not in outcomes


def test_the_cron_line_runs_the_quick_tick_every_minute():
    line = tick.command({"PATH": "/usr/bin"})
    assert line.startswith("* * * * * ") and "quick tick" in line and line.endswith(tick.MARKER)


@pytest.mark.feature("quick-swarm")
def test_the_launch_lands_in_the_seat_pane_with_the_quick_variables(tmp_path):
    swarm = _swarm(tmp_path)
    seat = swarm.seats[1]
    argv = launch.argv(swarm, seat, tmp_path / "opening.md")
    expected = "--host herdr --pane w1:p2 --name quick-abc-eng-1 --agent codex --dir /work --prompt-file"
    assert argv == [*expected.split(), str(tmp_path / "opening.md")]
    env = launch.environ({"PATH": "/bin", "AGENTIHOOKS_SWARM": "x"}, tmp_path, swarm, seat)
    assert env[member.QUICK_ENV] == "quick-abc" and env[member.SEAT_ENV] == "eng-1"
    assert env[member.DIR_ENV] == str(tmp_path) and "AGENTIHOOKS_SWARM" not in env and env["PATH"] == "/bin"


def test_a_successor_opening_carries_its_seed_and_the_handoff(tmp_path):
    swarm = _swarm(tmp_path, eng_state=registry.LAUNCHING)
    paths.handoff(tmp_path, "eng-1").write_text("# Handoff v2\nthe body\n")
    text = launch.opening(member.Member(tmp_path, swarm, swarm.seats[1]))
    assert "eng-1 of quick swarm quick-abc" in text and "the body" in text
    assert text.index("eng-1 of quick swarm") < text.index("the body")


@pytest.mark.feature("quick-swarm")
def test_relaunch_starts_the_successor_once_and_marks_the_seat_ready(tmp_path):
    _swarm(tmp_path, eng_state=registry.PENDING)
    paths.handoff(tmp_path, "eng-1").write_text("# Handoff v2\nthe body\n")
    started: list[list[str]] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append(argv)
        return 0

    assert launch.relaunch(tmp_path, "eng-1", {}, bare=lambda pane: True, start=start) == "started"
    assert launch.relaunch(tmp_path, "eng-1", {}, bare=lambda pane: True, start=start) == "not-pending"
    assert len(started) == 1 and "the body" in paths.opening(tmp_path, "eng-1").read_text()
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.READY


def test_a_failed_relaunch_or_a_pane_that_never_frees_leaves_the_seat_pending(tmp_path):
    _swarm(tmp_path, eng_state=registry.PENDING)
    assert launch.relaunch(tmp_path, "eng-1", {}, bare=lambda pane: True, start=lambda a, e: 2) == "failed-2"
    outcome = launch.relaunch(
        tmp_path, "eng-1", {}, bare=lambda pane: False, start=lambda a, e: 0, wait_s=0.0, sleep=lambda s: None
    )
    assert outcome == "pane-busy"
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.PENDING
