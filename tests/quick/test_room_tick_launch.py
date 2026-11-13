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
    def __init__(self, status: str = "idle", typed: str = "", bare: bool | None = False) -> None:
        self._status, self._typed, self._bare = status, typed, bare
        self.prompts: list[tuple[str, str]] = []

    def status(self, pane_id: str) -> str:
        return self._status

    def typed(self, pane_id: str) -> str:
        return self._typed

    def bare(self, pane_id: str) -> bool | None:
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


def test_a_seat_that_read_the_room_is_told_again_about_the_next_line(tmp_path):
    _swarm(tmp_path)
    herdr = FakeHerdr()
    room.post(tmp_path, "eng-1", "master", "one", 1)
    room.post(tmp_path, "eng-1", "master", "two", 2)
    _round(tmp_path, herdr)
    room.unread(tmp_path, "master")
    _round(tmp_path, herdr)
    room.post(tmp_path, "eng-1", "master", "three", 3)
    _round(tmp_path, herdr)
    assert herdr.prompts == [("w1:p1", tick.ROOM.format(count=2)), ("w1:p1", tick.ROOM.format(count=1))]


class RefusingHerdr(FakeHerdr):
    def prompt(self, pane_id: str, text: str) -> bool:
        self.prompts.append((pane_id, text))
        return False


def test_a_prompt_herdr_refuses_is_retried_on_the_next_round(tmp_path):
    _swarm(tmp_path)
    paths.plan(tmp_path, "eng-1").write_text("- [ ] two\n")
    room.post(tmp_path, "eng-1", "master", "hello", 1)
    herdr = RefusingHerdr()
    _round(tmp_path, herdr, now=10_000.0)
    _round(tmp_path, herdr, now=10_001.0)
    assert herdr.prompts.count(("w1:p1", tick.ROOM.format(count=1))) == 2
    assert herdr.prompts.count(("w1:p2", tick.NUDGE)) == 2


def test_tell_skips_a_busy_seat_and_tells_an_idle_one(tmp_path):
    swarm = _swarm(tmp_path)
    room.post(tmp_path, "eng-1", "master", "hello", 1)
    assert tick.tell(tmp_path, swarm.seats[0], FakeHerdr(status="working")) == "skipped-working"
    assert tick.tell(tmp_path, swarm.seats[0], FakeHerdr()) == "room"


@pytest.mark.feature("quick-swarm")
def test_a_pane_left_at_a_bare_shell_is_never_typed_into(tmp_path: Path) -> None:
    swarm = _swarm(tmp_path)
    room.post(tmp_path, "eng-1", "master", "plan ready", 100)
    room.post(tmp_path, "master", "eng-1", "go ahead", 101)
    herdr = FakeHerdr(bare=True)
    assert tick.tell(tmp_path, swarm.seats[0], herdr) == "skipped-bare"
    assert tick.tell(tmp_path, swarm.seats[1], herdr) == "skipped-bare"
    assert herdr.prompts == []


@pytest.mark.feature("quick-swarm")
def test_an_engineer_whose_agent_exited_starts_again_and_the_master_never_does(tmp_path: Path) -> None:
    _swarm(tmp_path)
    paths.plan(tmp_path, "eng-1").write_text("- [ ] stage one\n")
    herdr = FakeHerdr(bare=True)
    assert _round(tmp_path, herdr) == [
        {"seat": "master", "outcome": "skipped-bare"},
        {"seat": "eng-1", "outcome": "revived-started"},
        {"relaunched": "eng-1"},
    ]
    swarm = registry.load(tmp_path)
    assert herdr.prompts == [] and swarm is not None and swarm.seats[1].state == registry.PENDING


@pytest.mark.feature("quick-swarm")
def test_a_pane_herdr_cannot_read_is_never_typed_into_nor_relaunched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def gone(pane_id: str, environ: dict[str, str]) -> dict[str, object]:
        raise tick.herdr_host.HerdrError("pane not found")

    def late(pane_id: str, environ: dict[str, str]) -> dict[str, object]:
        raise tick.subprocess.TimeoutExpired("herdr", 30)

    monkeypatch.setattr(tick.herdr_host, "process_info", gone)
    assert tick.LiveHerdr({}).bare("w1:p9") is None
    monkeypatch.setattr(tick.herdr_host, "process_info", late)
    assert tick.LiveHerdr({}).bare("w1:p2") is None
    monkeypatch.setattr(tick.herdr_host, "process_info", lambda pane_id, environ: {})
    assert tick.LiveHerdr({}).bare("w1:p2") is None
    shell = {"foreground_processes": [{"name": "bash"}]}
    monkeypatch.setattr(tick.herdr_host, "process_info", lambda pane_id, environ: shell)
    assert tick.LiveHerdr({}).bare("w1:p2") is True
    swarm = _swarm(tmp_path)
    room.post(tmp_path, "master", "eng-1", "go ahead", 101)
    herdr = FakeHerdr(bare=None)
    assert tick.tell(tmp_path, swarm.seats[1], herdr) == "skipped-unknown"
    assert _round(tmp_path, herdr) == [
        {"seat": "master", "outcome": "skipped-unknown"},
        {"seat": "eng-1", "outcome": "skipped-unknown"},
    ]
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    assert _round(tmp_path, herdr) == [
        {"seat": "master", "outcome": "skipped-unknown"},
        {"seat": "eng-1", "outcome": "pending"},
    ]
    assert herdr.prompts == []


@pytest.mark.feature("quick-swarm")
def test_up_names_the_engineers_whose_agent_exited_at_launch(tmp_path: Path) -> None:
    swarm = _swarm(tmp_path)
    seen: list[str] = []
    naps: list[float] = []

    def bare(pane_id: str) -> bool:
        seen.append(pane_id)
        return len(seen) > 2

    assert launch.exited(swarm.seats[1:], bare, wait_s=3.0, sleep=naps.append) == ["eng-1"]
    assert seen == ["w1:p2", "w1:p2", "w1:p2"] and naps == [launch.POLL_S] * 3


@pytest.mark.feature("quick-swarm")
def test_up_watches_engineers_that_keep_running_for_the_whole_twenty_seconds(tmp_path: Path) -> None:
    swarm = _swarm(tmp_path)
    naps: list[float] = []
    assert launch.exited(swarm.seats[1:], lambda pane_id: False, wait_s=3.0, sleep=naps.append) == []
    assert naps == [launch.POLL_S] * 3
    naps.clear()
    assert launch.exited(swarm.seats[1:], lambda pane_id: False, sleep=naps.append) == []
    assert sum(naps) == 20.0


@pytest.mark.feature("quick-swarm")
def test_the_hourly_safety_round_runs_the_quick_tick(monkeypatch, tmp_path):
    from scripts.swarm import safety_nudge

    _swarm(tmp_path)
    environ = {"AGENTIHOOKS_HOME": str(tmp_path / "home")}
    paths.register(environ, "quick-abc", tmp_path)
    paths.plan(tmp_path, "eng-1").write_text("- [ ] two\n")
    herdr = FakeHerdr()
    monkeypatch.setattr(tick, "LiveHerdr", lambda env: herdr)
    rows = safety_nudge.run_round(environ, now=1)
    assert {"folder": str(tmp_path), "seat": "eng-1", "outcome": "nudged"} in rows
    assert herdr.prompts == [("w1:p2", tick.NUDGE)]


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
def test_relaunch_starts_the_successor_once_and_marks_the_seat_ready(tmp_path: Path) -> None:
    _swarm(tmp_path, eng_state=registry.PENDING)
    paths.handoff(tmp_path, "eng-1").write_text("# Handoff v2\nthe body\n")
    started: list[list[str]] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append(argv)
        return 0

    naps: list[float] = []

    def bare(pane: str) -> bool:
        assert naps or not started
        return not started

    assert launch.relaunch(tmp_path, "eng-1", {}, bare=bare, start=start, wait_s=0.0, sleep=naps.append) == "started"
    second = launch.relaunch(tmp_path, "eng-1", {}, bare=bare, start=start, wait_s=0.0, sleep=naps.append)
    assert second == "not-pending"
    assert len(started) == 1 and "the body" in paths.opening(tmp_path, "eng-1").read_text()
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.READY


def test_a_relaunch_that_raises_leaves_the_seat_pending(tmp_path: Path) -> None:
    _swarm(tmp_path, eng_state=registry.PENDING)

    def start(argv: list[str], environ: dict[str, str]) -> int:
        raise OSError("init-agent is missing")

    with pytest.raises(OSError):
        launch.relaunch(tmp_path, "eng-1", {}, bare=lambda pane: True, start=start, wait_s=0.0, sleep=lambda s: None)
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.PENDING


def test_a_failed_relaunch_or_a_pane_that_never_frees_leaves_the_seat_pending(tmp_path: Path) -> None:
    _swarm(tmp_path, eng_state=registry.PENDING)
    failed = launch.relaunch(
        tmp_path, "eng-1", {}, bare=lambda pane: True, start=lambda a, e: 2, wait_s=0.0, sleep=lambda s: None
    )
    assert failed == "failed-2"
    outcome = launch.relaunch(
        tmp_path, "eng-1", {}, bare=lambda pane: False, start=lambda a, e: 0, wait_s=0.0, sleep=lambda s: None
    )
    assert outcome == "pane-busy"
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.PENDING


@pytest.mark.feature("quick-swarm")
def test_a_successor_whose_agent_exits_at_once_leaves_the_seat_pending(tmp_path: Path) -> None:
    _swarm(tmp_path, eng_state=registry.PENDING)
    states: list[str] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        states.append("started")
        return 0

    def bare(pane: str) -> bool:
        loaded = registry.load(tmp_path)
        seat = loaded.seat("eng-1") if loaded else None
        states.append(seat.state if seat else "gone")
        return True

    outcome = launch.relaunch(tmp_path, "eng-1", {}, bare=bare, start=start, wait_s=0.0, sleep=lambda s: None)
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert outcome == "exited" and seat is not None and seat.state == registry.PENDING
    assert states == [registry.LAUNCHING, "started", registry.LAUNCHING]


@pytest.mark.feature("quick-swarm")
def test_a_successor_that_keeps_running_is_watched_the_whole_settle_window_while_launching(tmp_path: Path) -> None:
    _swarm(tmp_path, eng_state=registry.PENDING)
    started: list[str] = []
    naps: list[float] = []

    def bare(pane: str) -> bool:
        loaded = registry.load(tmp_path)
        seat = loaded.seat("eng-1") if loaded else None
        assert seat is not None and seat.state == registry.LAUNCHING
        assert naps or not started
        return not started

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append("eng-1")
        return 0

    assert launch.relaunch(tmp_path, "eng-1", {}, bare=bare, start=start, wait_s=0.0, sleep=naps.append) == "started"
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.READY and sum(naps) == launch.SETTLE_S == 20.0
