import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import herdr_host
from scripts.quick import cli, guard, member, paths, registry, room, tick

pytestmark = pytest.mark.unit


class FakePanes:
    def __init__(self, directory: Path, environ: dict[str, str]) -> None:
        self.rects = {"w1:p1": (0, 160)}

    def split(self, pane_id: str, ratio: float) -> str:
        x, width = self.rects[pane_id]
        kept = round(width * ratio)
        new = f"w1:p{len(self.rects) + 1}"
        self.rects[pane_id], self.rects[new] = (x, kept), (x + kept, width - kept)
        return new

    def columns(self, pane_id: str) -> dict[str, tuple[int, int]]:
        return dict(self.rects)


class IdleHerdr:
    prompts: list[tuple[str, str]] = []

    def status(self, pane_id: str) -> str:
        return "idle"

    def typed(self, pane_id: str) -> str:
        return ""

    def bare(self, pane_id: str) -> bool:
        return False

    def prompt(self, pane_id: str, text: str) -> bool:
        IdleHerdr.prompts.append((pane_id, text))
        return True


@pytest.fixture
def launched() -> list[str]:
    return []


@pytest.fixture
def quick_env(monkeypatch, tmp_path, launched) -> dict[str, str]:
    def start_all(folder: Path, swarm: registry.Swarm, environ: dict[str, str]) -> dict[str, int]:
        launched.extend(seat.seat for seat in swarm.seats[1:])
        return {seat.seat: 0 for seat in swarm.seats[1:]}

    monkeypatch.setattr(cli.guard, "require", lambda environ: "w1:p1")
    monkeypatch.setattr(cli.layout, "HerdrPanes", FakePanes)
    monkeypatch.setattr(cli.launch, "start_all", start_all)
    monkeypatch.setattr(cli.launch, "exited", lambda seats, bare: [])
    monkeypatch.setattr(cli.safety_nudge, "install", lambda environ: launched.append("cron") or "line")
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: IdleHerdr())
    monkeypatch.setattr(cli.paths.herdr_host, "repo_label", lambda directory: "repo")
    return {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / ".agentihooks"),
        "CLAUDE_CODE_SESSION_ID": "d039e169-c93c-4613-a495-d6dfb5284ff5",
        "HERDR_PANE_ID": "w1:p1",
    }


@pytest.mark.feature("quick-swarm")
def test_up_seats_the_caller_as_master_and_launches_the_engineers(quick_env, launched, tmp_path, capsys):
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 0
    assert launched == ["cron", "eng-1", "eng-2", "eng-3"]
    folder = tmp_path / "scratchpad" / "repo" / "quick-d039e169"
    swarm = registry.load(folder)
    assert swarm is not None and swarm.quick_id == "quick-d039e169" and swarm.directory == str(tmp_path)
    assert [(s.position, s.seat, s.pane_id) for s in swarm.seats] == [
        (1, "master", "w1:p1"),
        (2, "eng-1", "w1:p2"),
        (3, "eng-2", "w1:p3"),
        (4, "eng-3", "w1:p4"),
    ]
    assert swarm.seats[0].session_id == quick_env["CLAUDE_CODE_SESSION_ID"]
    assert [s.name for s in swarm.seats] == [f"quick-d039e169-{s}" for s in ("master", "eng-1", "eng-2", "eng-3")]
    assert paths.folders(quick_env) == [folder]
    assert paths.plan(folder, "eng-3").is_file()
    assert "quick-d039e169" in capsys.readouterr().out


def test_up_gives_each_engineer_its_harness(quick_env, tmp_path):
    assert cli.main(["up", "--agents", "3", "--harness", "codex,claude", "--dir", str(tmp_path)], quick_env) == 0
    swarm = registry.load(tmp_path / "scratchpad" / "repo" / "quick-d039e169")
    assert swarm is not None and [s.harness for s in swarm.seats] == ["claude", "codex", "claude"]


def test_up_refuses_a_harness_list_that_does_not_match_the_engineers(quick_env, tmp_path, capsys):
    assert cli.main(["up", "--agents", "3", "--harness", "codex,claude,claude", "--dir", str(tmp_path)], quick_env) == 2
    assert "2 engineers" in capsys.readouterr().err


@pytest.mark.feature("quick-swarm")
def test_up_outside_herdr_fails_loudly(monkeypatch, quick_env, tmp_path, capsys):
    def refuse(environ):
        raise guard.QuickError(guard.NOT_INSTALLED)

    monkeypatch.setattr(cli.guard, "require", refuse)
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 2
    assert guard.NOT_INSTALLED in capsys.readouterr().err


@pytest.mark.feature("quick-swarm")
def test_an_engineer_that_fails_to_start_fails_up_and_waits_for_a_relaunch(monkeypatch, quick_env, tmp_path, capsys):
    monkeypatch.setattr(cli.launch, "start_all", lambda folder, swarm, environ: {"eng-1": 0, "eng-2": 3, "eng-3": 0})
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 1
    swarm = registry.load(tmp_path / "scratchpad" / "repo" / "quick-d039e169")
    assert swarm is not None and [s.state for s in swarm.seats] == ["ready", "ready", "handoff", "ready"]
    assert "eng-2 did not start" in capsys.readouterr().err


@pytest.mark.feature("quick-swarm")
def test_an_engineer_whose_agent_exits_at_launch_fails_up_and_waits_for_a_relaunch(
    monkeypatch: pytest.MonkeyPatch, quick_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watched: list[str] = []

    def exited(seats: list[registry.Seat], bare: Callable[[str], bool]) -> list[str]:
        watched.extend(seat.seat for seat in seats if not bare(seat.pane_id))
        return ["eng-3"]

    monkeypatch.setattr(cli.launch, "exited", exited)
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 1
    swarm = registry.load(tmp_path / "scratchpad" / "repo" / "quick-d039e169")
    assert swarm is not None and [s.state for s in swarm.seats] == ["ready", "ready", "ready", "handoff"]
    assert watched == ["eng-1", "eng-2", "eng-3"]
    assert "eng-3 did not start" in capsys.readouterr().err


def test_a_cron_table_that_refuses_the_round_fails_up_in_one_line(monkeypatch, quick_env, tmp_path, capsys):
    def refuse(environ: dict[str, str]) -> str:
        raise RuntimeError("the cron table refused the entry")

    monkeypatch.setattr(cli.safety_nudge, "install", refuse)
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 2
    assert capsys.readouterr().err == "agentihooks quick: the cron table refused the entry\n"
    assert registry.load(tmp_path / "scratchpad" / "repo" / "quick-d039e169") is None


def test_up_twice_is_refused(quick_env, tmp_path, capsys):
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 0
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 2
    assert "already up" in capsys.readouterr().err


def test_send_posts_to_the_room_and_room_reads_it(quick_env, tmp_path, capsys):
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 0
    folder = tmp_path / "scratchpad" / "repo" / "quick-d039e169"
    engineer = {**quick_env, member.DIR_ENV: str(folder), member.SEAT_ENV: "eng-2", "HERDR_PANE_ID": "w1:p2"}
    IdleHerdr.prompts = []
    assert cli.main(["send", "master", "plan ready"], engineer) == 0
    assert "posted to master: room" in capsys.readouterr().out
    assert IdleHerdr.prompts == [("w1:p1", tick.ROOM.format(count=1))]
    assert cli.main(["room"], quick_env) == 0
    assert "eng-2 -> master: plan ready" in capsys.readouterr().out
    assert room.waiting(folder, "master") == 0


def test_send_to_an_unknown_seat_is_refused(quick_env, tmp_path, capsys):
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 0
    assert cli.main(["send", "eng-9", "hello"], quick_env) == 2
    assert "eng-9" in capsys.readouterr().err


def test_commands_outside_a_quick_swarm_are_refused(tmp_path, capsys):
    environ = {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "h")}
    for argv in (["send", "master", "x"], ["room"], ["handoff"], ["status"]):
        assert cli.main(argv, environ) == 2
    assert "not a member of a quick swarm" in capsys.readouterr().err


@pytest.mark.feature("quick-swarm")
def test_stop_forgets_the_swarm_and_its_room_but_keeps_the_seat_folders(quick_env, tmp_path):
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 0
    folder = tmp_path / "scratchpad" / "repo" / "quick-d039e169"
    room.post(folder, "eng-1", "master", "bye", 1)
    assert cli.main(["stop"], quick_env) == 0
    assert registry.load(folder) is None and paths.folders(quick_env) == []
    assert not paths.room(folder).exists() and paths.plan(folder, "eng-1").is_file()


def test_status_lists_every_seat(quick_env, tmp_path, capsys):
    assert cli.main(["up", "--dir", str(tmp_path)], quick_env) == 0
    capsys.readouterr()
    assert cli.main(["status"], quick_env) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["seat"] for row in rows] == ["master", "eng-1", "eng-2", "eng-3"]


def test_herdr_columns_read_the_tab_layout(monkeypatch):
    layout = {"layout": {"panes": [{"pane_id": "w1:p1", "rect": {"x": 0, "width": 80}}]}}
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: layout)
    assert herdr_host.columns("w1:p1", {}) == {"w1:p1": (0, 80)}


def test_herdr_split_right_returns_the_new_pane(monkeypatch, tmp_path):
    seen: list[list[str]] = []
    pane = {"pane": {"workspace_id": "w1", "tab_id": "t1", "pane_id": "w1:p2", "terminal_id": "term"}}
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: seen.append(args) or pane)
    assert herdr_host.split_right("w1:p1", tmp_path, {}, 0.25).pane_id == "w1:p2"
    assert seen == [
        ["pane", "split", "w1:p1", "--direction", "right", "--ratio", "0.25", "--cwd", str(tmp_path), "--no-focus"]
    ]


def test_herdr_existing_pane_and_run_with_variables(monkeypatch, tmp_path):
    seen: list[list[str]] = []
    pane = {"pane": {"workspace_id": "w1", "tab_id": "t1", "pane_id": "w1:p2"}}
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: seen.append(args) or pane)
    assert herdr_host.existing_pane("w1:p2", {}).tab_id == "t1"
    herdr_host.run("w1:p2", tmp_path / "l.sh", {}, {"HERDR_AGENT": "codex"})
    assert seen[-1] == ["pane", "run", "w1:p2", f"HERDR_AGENT=codex exec {tmp_path / 'l.sh'}"]
    herdr_host.run("w1:p2", tmp_path / "l.sh", {})
    assert seen[-1] == ["pane", "run", "w1:p2", f"exec {tmp_path / 'l.sh'}"]


def test_herdr_bare_shell_reads_the_pane_processes(monkeypatch):
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {"foreground_processes": [{"name": "bash"}]})
    assert isinstance(herdr_host.process_info("w1:p2", {}), dict)
