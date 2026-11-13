"""Mutation-kill tests for scripts.quick launch, tick, room, registry, layout, handoff and guard."""

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from scripts.handoff import resolve
from scripts.quick import guard, handoff, launch, layout, paths, registry, room, tick
from tests.quick.test_room_tick_launch import FakeHerdr, _swarm

pytestmark = pytest.mark.unit


def test_require_passes_the_environ_mapping_to_running() -> None:
    seen: list[dict[str, str]] = []

    def running(env: dict[str, str]) -> bool:
        seen.append(env)
        return True

    found = guard.require({"HERDR_PANE_ID": "w1:p1", "X": "1"}, binary=lambda: "/bin/herdr", running=running)
    assert found == "w1:p1"
    assert seen == [{"HERDR_PANE_ID": "w1:p1", "X": "1"}]


def test_split_calls_split_right_with_pane_directory_environ_and_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path, dict[str, str], float]] = []

    class Result:
        pane_id = "w1:new"

    def fake_split_right(pane_id: str, directory: Path, environ: dict[str, str], ratio: float) -> Result:
        calls.append((pane_id, directory, environ, ratio))
        return Result()

    monkeypatch.setattr(layout.herdr_host, "split_right", fake_split_right)
    panes = layout.HerdrPanes(Path("/work"), {"K": "V"})
    assert panes.split("w1:p1", 0.5) == "w1:new"
    assert calls == [("w1:p1", Path("/work"), {"K": "V"}, 0.5)]


def test_columns_calls_herdr_columns_with_pane_and_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_columns(pane_id: str, environ: dict[str, str]) -> dict[str, tuple[int, int]]:
        calls.append((pane_id, environ))
        return {"w1:p1": (0, 10)}

    monkeypatch.setattr(layout.herdr_host, "columns", fake_columns)
    panes = layout.HerdrPanes(Path("/work"), {"K": "V"})
    assert panes.columns("w1:p1") == {"w1:p1": (0, 10)}
    assert calls == [("w1:p1", {"K": "V"})]


class _MisnamedPanes:
    def __init__(self) -> None:
        self.rects: dict[str, tuple[int, int]] = {"w1:p1": (0, 1)}
        self._plan = iter([("w1:zzz", 0), ("w1:aaa", 100)])
        self.columns_calls: list[str] = []

    def split(self, pane_id: str, ratio: float) -> str:
        new, x = next(self._plan)
        self.rects[new] = (x, 1)
        return new

    def columns(self, pane_id: str) -> dict[str, tuple[int, int]]:
        self.columns_calls.append(pane_id)
        return dict(self.rects)


def test_build_orders_new_panes_by_column_not_name_and_asks_columns_for_the_caller() -> None:
    panes = _MisnamedPanes()
    ordered = layout.build("w1:p1", 3, panes)
    assert ordered == ["w1:p1", "w1:zzz", "w1:aaa"]
    assert panes.columns_calls == ["w1:p1"]


def test_an_https_address_resolves_through_resolver_with_its_exact_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeResolver:
        def __init__(self, slug: str, redis: object) -> None:
            captured["ctor"] = (slug, redis)

        def __call__(self, address: str) -> bool:
            captured["call"] = address
            return True

    monkeypatch.setattr(resolve, "Resolver", FakeResolver)
    resolves = handoff.resolver(tmp_path)
    assert resolves("https://github.com/x/y") is True
    assert captured["ctor"] == ("", None)
    assert captured["call"] == "https://github.com/x/y"


def test_save_writes_json_with_two_space_indentation(tmp_path: Path) -> None:
    swarm = registry.Swarm("quick-abc", "/work", (registry.Seat(1, "master", "w1:p1", "claude", "n"),))
    registry.save(tmp_path, swarm)
    expected = json.dumps(dataclasses.asdict(swarm), indent=2)
    assert registry.path(tmp_path).read_text() == expected


def test_save_creates_missing_parent_directories(tmp_path: Path) -> None:
    deep = tmp_path / "missing" / "nested"
    swarm = registry.Swarm("quick-abc", "/work", (registry.Seat(1, "master", "w1:p1", "claude", "n"),))
    registry.save(deep, swarm)
    assert registry.load(deep) == swarm


def test_locked_creates_missing_parent_directories(tmp_path: Path) -> None:
    deep = tmp_path / "missing" / "nested"
    with registry.locked(deep):
        pass
    assert deep.is_dir()


def test_post_creates_missing_parent_directories(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    line = room.post(deep, "eng-1", "master", "hi", 1)
    assert line["text"] == "hi"
    assert paths.room(deep).read_text().strip() != ""


def test_unread_marks_its_cursor_file_room_cursor(tmp_path: Path) -> None:
    paths.create(tmp_path, ["master"])
    room.post(tmp_path, "eng-1", "master", "hi", 1)
    room.unread(tmp_path, "master")
    assert (paths.seat_dir(tmp_path, "master") / "room.cursor").read_text() == "1"


def test_waiting_treats_a_blank_cursor_file_as_unread_from_the_start(tmp_path: Path) -> None:
    paths.create(tmp_path, ["master"])
    room.post(tmp_path, "eng-1", "master", "one", 1)
    room.post(tmp_path, "eng-1", "master", "two", 2)
    cursor = paths.seat_dir(tmp_path, "master") / "room.cursor"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text("  ")
    assert room.waiting(tmp_path, "master") == 2


def test_unread_creates_missing_cursor_directory(tmp_path: Path) -> None:
    deep = tmp_path / "x" / "y"
    room.post(deep, "eng-1", "master", "hi", 1)
    mine = room.unread(deep, "master")
    assert mine and mine[0]["text"] == "hi"


def test_number_treats_a_blank_stamp_file_as_zero(tmp_path: Path) -> None:
    stamp = tmp_path / "stamp"
    stamp.write_text("   ")
    assert tick._number(stamp) == 0.0


class _BusySpy:
    def __init__(self, status: str = "idle", typed: str = "half a line", bare: bool = False) -> None:
        self.bare_calls: list[str] = []
        self.status_calls: list[str] = []
        self.typed_calls: list[str] = []
        self._status, self._typed, self._bare = status, typed, bare

    def bare(self, pane_id: str) -> bool | None:
        self.bare_calls.append(pane_id)
        return self._bare

    def status(self, pane_id: str) -> str:
        self.status_calls.append(pane_id)
        return self._status

    def typed(self, pane_id: str) -> str:
        self.typed_calls.append(pane_id)
        return self._typed

    def prompt(self, pane_id: str, text: str) -> bool:
        return True


def test_busy_reports_skipped_typed_for_this_exact_pane() -> None:
    seat = registry.Seat(1, "eng-1", "w1:p7", "claude", "n")
    spy = _BusySpy()
    assert tick._busy(seat, spy) == "skipped-typed"
    assert spy.bare_calls == ["w1:p7"]
    assert spy.status_calls == ["w1:p7"]
    assert spy.typed_calls == ["w1:p7"]


def test_ready_seat_reports_room_when_a_notice_was_sent(tmp_path: Path) -> None:
    paths.create(tmp_path, ["master"])
    room.post(tmp_path, "eng-1", "master", "hi", 1)
    seat = registry.Seat(1, "master", "w1:p1", "claude", "n")
    assert tick._ready_seat(tmp_path, seat, FakeHerdr(), 0.0) == "room"


def test_nudge_writes_the_nudged_at_stamp_file(tmp_path: Path) -> None:
    paths.create(tmp_path, ["eng-1"])
    paths.plan(tmp_path, "eng-1").write_text("- [ ] two\n")
    seat = registry.Seat(2, "eng-1", "w1:p2", "codex", "n")
    assert tick._nudge(tmp_path, seat, FakeHerdr(), 10_000.0) is True
    assert (paths.seat_dir(tmp_path, "eng-1") / "nudged.at").read_text() == "10000.0"


def test_room_notice_resets_the_told_stamp_when_the_count_drops(tmp_path: Path) -> None:
    paths.create(tmp_path, ["eng-1"])
    seat = registry.Seat(2, "eng-1", "w1:p2", "codex", "n")
    told = paths.seat_dir(tmp_path, "eng-1") / "room.told"
    told.parent.mkdir(parents=True, exist_ok=True)
    told.write_text("5")
    assert tick._room_notice(tmp_path, seat, FakeHerdr()) is False
    assert told.read_text() == "0"


def test_room_notice_only_resets_the_stamp_when_the_count_is_strictly_lower(tmp_path: Path) -> None:
    paths.create(tmp_path, ["eng-1"])
    seat = registry.Seat(2, "eng-1", "w1:p2", "codex", "n")
    told = paths.seat_dir(tmp_path, "eng-1") / "room.told"
    told.parent.mkdir(parents=True, exist_ok=True)
    told.write_text("3.0")
    room.post(tmp_path, "master", "eng-1", "one", 1)
    room.post(tmp_path, "master", "eng-1", "two", 2)
    room.post(tmp_path, "master", "eng-1", "three", 3)
    assert tick._room_notice(tmp_path, seat, FakeHerdr()) is False
    assert told.read_text() == "3.0"


def test_tell_reports_idle_when_nothing_is_waiting(tmp_path: Path) -> None:
    paths.create(tmp_path, ["master"])
    seat = registry.Seat(1, "master", "w1:p1", "claude", "n")
    assert tick.tell(tmp_path, seat, FakeHerdr()) == "idle"


class _BareSpy:
    def __init__(self, value: bool | None) -> None:
        self.calls: list[str] = []
        self._value = value

    def bare(self, pane_id: str) -> bool | None:
        self.calls.append(pane_id)
        return self._value

    def status(self, pane_id: str) -> str:
        return "idle"

    def typed(self, pane_id: str) -> str:
        return ""

    def prompt(self, pane_id: str, text: str) -> bool:
        return True


def test_outcome_relaunches_a_pending_seat_at_its_own_pane(tmp_path: Path) -> None:
    seat = registry.Seat(2, "eng-1", "w1:p2", "codex", "n", state=registry.PENDING)
    herdr = _BareSpy(True)
    relaunch_calls: list[tuple[Path, str]] = []

    def relaunch(folder: Path, name: str) -> str:
        relaunch_calls.append((folder, name))
        return "started"

    outcome = tick._outcome(tmp_path, seat, herdr, relaunch, 0.0)
    assert outcome == "relaunched-started"
    assert herdr.calls == ["w1:p2"]
    assert relaunch_calls == [(tmp_path, "eng-1")]


def test_outcome_revives_an_exited_engineer_at_its_own_pane(tmp_path: Path) -> None:
    paths.create(tmp_path, ["eng-1"])
    swarm = registry.Swarm("quick-abc", "/work", (registry.Seat(2, "eng-1", "w1:p2", "codex", "n"),))
    registry.save(tmp_path, swarm)
    seat = swarm.seats[0]
    herdr = _BareSpy(True)
    relaunch_calls: list[tuple[Path, str]] = []

    def relaunch(folder: Path, name: str) -> str:
        relaunch_calls.append((folder, name))
        return "started"

    outcome = tick._outcome(tmp_path, seat, herdr, relaunch, 0.0)
    assert outcome == "revived-started"
    assert herdr.calls == ["w1:p2"]
    assert relaunch_calls == [(tmp_path, "eng-1")]
    loaded = registry.load(tmp_path)
    assert loaded is not None
    revived = loaded.seat("eng-1")
    assert revived is not None and revived.state == registry.PENDING


def test_outcome_only_revives_a_seat_whose_registry_state_is_still_ready(tmp_path: Path) -> None:
    paths.create(tmp_path, ["eng-1"])
    swarm = registry.Swarm("quick-abc", "/work", (registry.Seat(2, "eng-1", "w1:p2", "codex", "n"),))
    registry.save(tmp_path, swarm)
    stale_seat = swarm.seats[0]
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    relaunch_calls: list[tuple[Path, str]] = []

    def relaunch(folder: Path, name: str) -> str:
        relaunch_calls.append((folder, name))
        return "started"

    outcome = tick._outcome(tmp_path, stale_seat, FakeHerdr(bare=True), relaunch, 0.0)
    assert relaunch_calls == [] and not outcome.startswith("revived")


def test_live_herdr_methods_pass_pane_and_environ_to_herdr_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, tuple[object, ...]] = {}

    def fake_agent_status(pane_id: str, environ: dict[str, str]) -> str:
        calls["status"] = (pane_id, environ)
        return "idle"

    def fake_typed(pane_id: str, environ: dict[str, str]) -> str:
        calls["typed"] = (pane_id, environ)
        return "txt"

    def fake_prompt(pane_id: str, text: str, environ: dict[str, str]) -> bool:
        calls["prompt"] = (pane_id, text, environ)
        return True

    monkeypatch.setattr(tick.herdr_host, "agent_status", fake_agent_status)
    monkeypatch.setattr(tick.herdr_host, "typed", fake_typed)
    monkeypatch.setattr(tick.herdr_host, "prompt", fake_prompt)
    live = tick.LiveHerdr({"K": "V"})
    assert live.status("w1:p1") == "idle" and calls["status"] == ("w1:p1", {"K": "V"})
    assert live.typed("w1:p1") == "txt" and calls["typed"] == ("w1:p1", {"K": "V"})
    assert live.prompt("w1:p1", "hello") is True and calls["prompt"] == ("w1:p1", "hello", {"K": "V"})


def test_live_herdr_bare_asks_process_info_for_the_pane_and_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def fake_process_info(pane_id: str, environ: dict[str, str]) -> dict[str, object]:
        seen.append((pane_id, environ))
        return {"foreground_processes": [{"name": "bash"}]}

    monkeypatch.setattr(tick.herdr_host, "process_info", fake_process_info)
    assert tick.LiveHerdr({"K": "V"}).bare("w1:p9") is True
    assert seen == [("w1:p9", {"K": "V"})]


def test_run_round_builds_a_live_herdr_from_the_environ_when_none_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[dict[str, str]] = []

    def spy_live_herdr(env: dict[str, str]) -> FakeHerdr:
        captured.append(env)
        return FakeHerdr()

    monkeypatch.setattr(tick, "LiveHerdr", spy_live_herdr)
    environ = {"AGENTIHOOKS_HOME": str(tmp_path / "home"), "X": "1"}
    tick.run_round(environ)
    assert captured == [dict(environ)]


def test_run_round_wires_its_relaunch_arguments_into_round_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    swarm = _swarm(tmp_path, eng_state=registry.PENDING)
    environ = {"AGENTIHOOKS_HOME": str(tmp_path / "home")}
    paths.register(environ, swarm.quick_id, tmp_path)
    herdr = FakeHerdr(bare=True)
    calls: list[tuple[Path, str, dict[str, str], object, float]] = []

    def fake_relaunch(folder: Path, seat: str, base: dict[str, str], *, bare: object, wait_s: float) -> str:
        calls.append((folder, seat, base, bare, wait_s))
        return "started"

    def spy_live_herdr(env: dict[str, str]) -> FakeHerdr:
        return herdr

    monkeypatch.setattr(launch, "relaunch", fake_relaunch)
    monkeypatch.setattr(tick, "LiveHerdr", spy_live_herdr)
    rows = tick.run_round(environ)
    assert {"folder": str(tmp_path), "seat": "eng-1", "outcome": "relaunched-started"} in rows
    assert calls == [(tmp_path, "eng-1", environ, herdr.bare, 0.0)]


def test_run_round_accumulates_rows_across_every_registered_quick_swarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder_a, folder_b = tmp_path / "a", tmp_path / "b"
    _swarm(folder_a)
    _swarm(folder_b)
    environ = {"AGENTIHOOKS_HOME": str(tmp_path / "home")}
    paths.register(environ, "quick-aaa", folder_a)
    paths.register(environ, "quick-bbb", folder_b)
    herdr = FakeHerdr()

    def spy_live_herdr(env: dict[str, str]) -> FakeHerdr:
        return herdr

    monkeypatch.setattr(tick, "LiveHerdr", spy_live_herdr)
    rows = tick.run_round(environ)
    seen_folders = {row["folder"] for row in rows}
    assert str(folder_a) in seen_folders and str(folder_b) in seen_folders


def test_exited_computes_rounds_from_wait_s_over_poll_s_not_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "POLL_S", 2.0)
    naps: list[float] = []
    seats = (registry.Seat(1, "eng-1", "w1:p1", "claude", "n"),)

    def never_bare(pane_id: str) -> bool:
        return False

    launch.exited(seats, never_bare, wait_s=6.0, sleep=naps.append)
    assert len(naps) == 3


def test_exited_remembers_seats_that_went_bare_in_an_earlier_round() -> None:
    seats = (
        registry.Seat(1, "eng-1", "w1:p1", "claude", "n1"),
        registry.Seat(2, "eng-2", "w1:p2", "claude", "n2"),
    )
    calls: dict[str, int] = {"w1:p1": 0, "w1:p2": 0}

    def bare(pane_id: str) -> bool:
        calls[pane_id] += 1
        if pane_id == "w1:p1":
            return calls[pane_id] >= 1
        return calls[pane_id] >= 2

    naps: list[float] = []
    result = launch.exited(seats, bare, wait_s=3.0, sleep=naps.append)
    assert result == ["eng-1", "eng-2"]


def test_relaunch_polls_the_right_pane_until_its_deadline_then_gives_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _swarm(tmp_path, eng_state=registry.PENDING)
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])

    def fake_monotonic() -> float:
        return next(clock)

    monkeypatch.setattr(launch.time, "monotonic", fake_monotonic)
    naps: list[float] = []
    seen: list[str] = []

    def bare(pane: str) -> bool:
        seen.append(pane)
        return False

    outcome = launch.relaunch(tmp_path, "eng-1", {}, bare=bare, wait_s=2.0, sleep=naps.append)
    assert outcome == "pane-busy"
    assert seen == ["w1:p2", "w1:p2"]
    assert naps == [launch.POLL_S]
    loaded = registry.load(tmp_path)
    assert loaded is not None
    seat = loaded.seat("eng-1")
    assert seat is not None and seat.state == registry.PENDING


def test_write_opening_creates_missing_seat_directories(tmp_path: Path) -> None:
    deep = tmp_path / "x" / "y"
    swarm = registry.Swarm("quick-abc", "/work", (registry.Seat(1, "master", "w1:p1", "claude", "n"),))
    target = launch._write_opening(launch.Member(deep, swarm, swarm.seats[0]))
    assert target.read_text()
    assert target == paths.opening(deep, "master")


def test_start_seat_opens_the_seat_with_its_argv_and_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    swarm = registry.Swarm(
        "quick-abc",
        "/work",
        (registry.Seat(1, "master", "w1:p1", "claude", "n0"), registry.Seat(2, "eng-1", "w1:p2", "codex", "n1")),
    )
    seat = swarm.seats[1]
    opening_path = tmp_path / "opening.md"

    def fake_write_opening(found: launch.Member) -> Path:
        return opening_path

    monkeypatch.setattr(launch, "_write_opening", fake_write_opening)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        calls.append((argv, environ))
        return 0

    base = {"PATH": "/bin"}
    code = launch.start_seat(tmp_path, swarm, seat, base, start)
    assert code == 0
    assert calls == [(launch.argv(swarm, seat, opening_path), launch.environ(base, tmp_path, swarm, seat))]


def test_init_agent_runs_the_init_agent_module_with_exact_argv_and_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        returncode = 7

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr(launch.subprocess, "run", fake_run)
    code = launch.init_agent(["--host", "herdr"], {"A": "1"})
    assert code == 7
    assert calls == [
        ([sys.executable, "-m", "scripts.init_agent", "--host", "herdr"], {"env": {"A": "1"}, "check": False})
    ]


def test_start_all_launches_every_engineer_with_its_own_prompt_file_and_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    swarm = registry.Swarm(
        "quick-abc",
        "/work",
        (
            registry.Seat(1, "master", "w1:p1", "claude", "n0"),
            registry.Seat(2, "eng-1", "w1:p2", "codex", "n1"),
            registry.Seat(3, "eng-2", "w1:p3", "claude", "n2"),
        ),
    )
    opened: list[launch.Member] = []

    def fake_write_opening(found: launch.Member) -> Path:
        opened.append(found)
        return tmp_path / f"{found.seat.seat}.md"

    popened: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        def __init__(self, code: int) -> None:
            self._code = code

        def wait(self) -> int:
            return self._code

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        popened.append((command, kwargs))
        return FakeProcess(0 if len(popened) == 1 else 3)

    monkeypatch.setattr(launch, "_write_opening", fake_write_opening)
    monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
    base = {"PATH": "/bin"}
    result = launch.start_all(tmp_path, swarm, base)
    assert opened == [launch.Member(tmp_path, swarm, swarm.seats[1]), launch.Member(tmp_path, swarm, swarm.seats[2])]
    assert [command for command, _ in popened] == [
        [sys.executable, "-m", "scripts.init_agent", *launch.argv(swarm, swarm.seats[1], tmp_path / "eng-1.md")],
        [sys.executable, "-m", "scripts.init_agent", *launch.argv(swarm, swarm.seats[2], tmp_path / "eng-2.md")],
    ]
    assert [kwargs["env"] for _, kwargs in popened] == [
        launch.environ(base, tmp_path, swarm, swarm.seats[1]),
        launch.environ(base, tmp_path, swarm, swarm.seats[2]),
    ]
    assert result == {"eng-1": 0, "eng-2": 3}
