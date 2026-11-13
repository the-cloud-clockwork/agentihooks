import argparse
import json
from pathlib import Path

import pytest

from scripts.quick import cli, layout, member, registry
from tests.quick.test_cli import FakePanes

pytestmark = pytest.mark.unit


def test_parser_declares_every_subcommand_with_its_exact_help_and_options() -> None:
    root = cli.parser()
    assert root.prog == "agentihooks quick"
    assert root.description == "A herdr-only master and engineers"
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    assert sub.dest == "command"
    assert sub.required is True
    help_text = {a.dest: a.help for a in sub._choices_actions}
    assert help_text == {
        "up": "Split this herdr pane's tab and launch the engineers; this session is master",
        "status": "Print every seat of this session's quick swarm",
        "handoff": "Accept this seat's Handoff v2 and start its successor in this pane",
        "relaunch": "Start the successor of a seat that handed off",
        "send": "Post a message to another seat through the room",
        "room": "Print the room messages sent to this seat since it last read",
        "tick": "Run one quick tick over every live quick swarm",
        "stop": "Stop this session's quick swarm, keeping its seat folders",
    }
    assert set(sub.choices) == set(help_text)

    up = sub.choices["up"]
    agents = up._option_string_actions["--agents"]
    assert (agents.type, agents.default, agents.help) == (int, layout.DEFAULT_AGENTS, "Agents with the master, 2 to 5")
    harness = up._option_string_actions["--harness"]
    assert (harness.default, harness.help) == ("claude", "Engineer harness, or one per engineer: claude,codex")
    directory = up._option_string_actions["--dir"]
    assert (directory.default, directory.help) == ("", "Directory the engineers work in; default the current one")

    given = sub.choices["handoff"]
    document = next(a for a in given._actions if a.dest == "document")
    assert (document.nargs, document.default, document.help) == ("?", "", "Handoff v2 file; default the seat's handoff")

    again = sub.choices["relaunch"]
    seat_arg = next(a for a in again._actions if a.dest == "seat")
    assert seat_arg.option_strings == []
    folder_arg = again._option_string_actions["--folder"]
    assert folder_arg.required is True

    send = sub.choices["send"]
    to_arg = next(a for a in send._actions if a.dest == "to")
    text_arg = next(a for a in send._actions if a.dest == "text")
    assert to_arg.option_strings == [] and text_arg.option_strings == []


def _namespace(agents: int = 4, harness: str = "claude") -> argparse.Namespace:
    return argparse.Namespace(agents=agents, harness=harness)


def test_swarm_for_reads_the_target_harness_and_the_raw_session_from_the_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.layout, "HerdrPanes", FakePanes)
    environ = {"AGENTIHOOKS_TARGET": "codex", "CLAUDE_CODE_SESSION_ID": "ABCDEF12-session"}
    swarm = cli._swarm_for(_namespace(agents=2), environ, "w1:p1", "quick-test", tmp_path)
    master = swarm.seats[0]
    assert master.harness == "codex"
    assert master.session_id == "ABCDEF12-session"


def test_swarm_for_defaults_the_harness_and_session_when_the_environ_lacks_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.layout, "HerdrPanes", FakePanes)
    swarm = cli._swarm_for(_namespace(agents=2), {}, "w1:p1", "quick-test", tmp_path)
    master = swarm.seats[0]
    assert master.harness == "claude"
    assert master.session_id == ""


def test_swarm_for_builds_its_panes_with_the_real_directory_and_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class SpyPanes(FakePanes):
        def __init__(self, directory: Path, environ: dict[str, str]) -> None:
            captured["directory"] = directory
            captured["environ"] = environ
            super().__init__(directory, environ)

    monkeypatch.setattr(cli.layout, "HerdrPanes", SpyPanes)
    environ = {"marker": "yes"}
    cli._swarm_for(_namespace(agents=2), environ, "w1:p1", "quick-test", tmp_path)
    assert captured == {"directory": tmp_path, "environ": environ}


def test_up_passes_its_real_arguments_to_every_collaborator_and_prints_exact_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, object] = {}
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / ".agentihooks"),
        "CLAUDE_CODE_SESSION_ID": "d039e169-c93c-4613-a495-d6dfb5284ff5",
    }
    folder = tmp_path / "quick-folder"

    def require(seen_environ: dict[str, str]) -> str:
        calls["require_environ"] = seen_environ
        return "w1:p1"

    def folder_for(seen_environ: dict[str, str], quick: str, directory: Path) -> Path:
        calls["folder_for_args"] = (seen_environ, quick, directory)
        return folder

    def install(seen_environ: dict[str, str]) -> None:
        calls["install_environ"] = seen_environ

    def start_all(seen_folder: Path, swarm: registry.Swarm, seen_environ: dict[str, str]) -> dict[str, int]:
        calls["start_all_args"] = (seen_folder, swarm, seen_environ)
        return {"eng-1": 0, "eng-2": 3, "eng-3": 0}

    def exited(seats: tuple[registry.Seat, ...], bare: object) -> list[str]:
        calls["exited_bare"] = bare
        return ["eng-1"]

    class SpyHerdr:
        def __init__(self, seen_environ: dict[str, str]) -> None:
            calls["live_herdr_environ"] = seen_environ

        def bare(self, pane_id: str) -> bool:
            return False

    monkeypatch.setattr(cli.guard, "require", require)
    monkeypatch.setattr(cli.layout, "HerdrPanes", FakePanes)
    monkeypatch.setattr(cli.paths, "folder_for", folder_for)
    monkeypatch.setattr(cli.safety_nudge, "install", install)
    monkeypatch.setattr(cli.launch, "start_all", start_all)
    monkeypatch.setattr(cli.launch, "exited", exited)
    monkeypatch.setattr(cli.tick, "LiveHerdr", SpyHerdr)

    assert cli.main(["up", "--dir", str(tmp_path)], environ) == 1

    assert calls["require_environ"] == environ
    assert calls["folder_for_args"] == (environ, "quick-d039e169", tmp_path.resolve())
    assert calls["install_environ"] == environ
    start_all_args = calls["start_all_args"]
    assert isinstance(start_all_args, tuple)
    assert start_all_args[0] == folder and start_all_args[2] == environ
    assert calls["live_herdr_environ"] == environ

    captured_out = capsys.readouterr()
    printed = captured_out.out.splitlines()
    summary = json.loads(printed[0])
    assert summary == {
        "quick_id": "quick-d039e169",
        "folder": str(folder),
        "started": {"eng-1": 0, "eng-2": 3, "eng-3": 0},
    }
    assert [json.loads(line)["seat"] for line in printed[1:]] == ["master", "eng-1", "eng-2", "eng-3"]
    assert captured_out.err == "agentihooks quick: eng-1, eng-2 did not start; the next round retries it\n"


def test_handoff_with_no_problems_reports_where_the_successor_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, object] = {}
    seat = registry.Seat(2, "eng-1", "w1:p3", "claude", "quick-abc-eng-1")
    swarm = registry.Swarm("quick-abc", "/work", (seat,))
    found = member.Member(tmp_path, swarm, seat)
    monkeypatch.setattr(cli, "_member", lambda environ: found)

    def accept(member_arg: member.Member, document_arg: Path | None) -> list[str]:
        calls["args"] = (member_arg, document_arg)
        return []

    monkeypatch.setattr(cli.handoff, "accept", accept)
    assert cli.main(["handoff"], {"HOME": str(tmp_path)}) == 0
    assert calls["args"] == (found, None)
    out = "handoff accepted: this session ends at its next stop and its successor starts in w1:p3\n"
    assert capsys.readouterr().out == out


def test_handoff_with_a_given_document_expands_its_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    seat = registry.Seat(2, "eng-1", "w1:p3", "claude", "quick-abc-eng-1")
    swarm = registry.Swarm("quick-abc", "/work", (seat,))
    found = member.Member(tmp_path, swarm, seat)
    monkeypatch.setattr(cli, "_member", lambda environ: found)

    def accept(member_arg: member.Member, document_arg: Path | None) -> list[str]:
        calls["document"] = document_arg
        return []

    monkeypatch.setattr(cli.handoff, "accept", accept)
    assert cli.main(["handoff", "notes/plan.md"], {"HOME": str(tmp_path)}) == 0
    assert calls["document"] == Path("notes/plan.md").expanduser()


def test_handoff_with_problems_refuses_and_lists_them_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seat = registry.Seat(2, "eng-1", "w1:p3", "claude", "quick-abc-eng-1")
    swarm = registry.Swarm("quick-abc", "/work", (seat,))
    found = member.Member(tmp_path, swarm, seat)
    monkeypatch.setattr(cli, "_member", lambda environ: found)
    monkeypatch.setattr(cli.handoff, "accept", lambda member_arg, document_arg: ["missing seams", "stale notes"])
    assert cli.main(["handoff"], {"HOME": str(tmp_path)}) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "handoff refused; fix the document and run the command again:\n- missing seams\n- stale notes\n"
    )


def test_relaunch_starts_the_successors_pending_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, object] = {}

    class Herdr:
        def bare(self, pane_id: str) -> bool:
            return True

    herdr = Herdr()

    def make_herdr(seen_environ: dict[str, str]) -> Herdr:
        calls["live_herdr_environ"] = seen_environ
        return herdr

    def relaunch(folder: Path, seat_name: str, base: dict[str, str], *, bare: object) -> str:
        calls["args"] = (folder, seat_name, base, bare)
        return "started"

    monkeypatch.setattr(cli.tick, "LiveHerdr", make_herdr)
    monkeypatch.setattr(cli.launch, "relaunch", relaunch)
    environ = {"HOME": str(tmp_path)}
    assert cli.main(["relaunch", "eng-1", "--folder", str(tmp_path)], environ) == 0
    assert calls["args"] == (tmp_path, "eng-1", environ, herdr.bare)
    assert calls["live_herdr_environ"] == environ
    assert capsys.readouterr().out == "started\n"


def test_relaunch_that_does_not_start_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Herdr:
        def bare(self, pane_id: str) -> bool:
            return True

    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: Herdr())
    monkeypatch.setattr(cli.launch, "relaunch", lambda folder, seat_name, base, *, bare: "not-pending")
    environ = {"HOME": str(tmp_path)}
    assert cli.main(["relaunch", "eng-1", "--folder", str(tmp_path)], environ) == 1
    assert capsys.readouterr().out == "not-pending\n"


def test_send_posts_with_the_current_time_and_a_live_herdr_built_from_the_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    master = registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master")
    eng = registry.Seat(2, "eng-1", "w1:p2", "claude", "quick-abc-eng-1")
    swarm = registry.Swarm("quick-abc", "/work", (master, eng))
    found = member.Member(tmp_path, swarm, eng)
    monkeypatch.setattr(cli, "_member", lambda environ: found)
    monkeypatch.setattr(cli.time, "time", lambda: 1700000000.0)
    posted: dict[str, object] = {}

    def post(folder: Path, sender: str, to: str, text: str, now: int) -> None:
        posted.update(folder=folder, sender=sender, to=to, text=text, now=now)

    monkeypatch.setattr(cli.room, "post", post)
    created: dict[str, object] = {}
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: created.setdefault("environ", environ) or object())
    monkeypatch.setattr(cli.tick, "tell", lambda folder, target, herdr: "ok")
    environ = {"HOME": str(tmp_path)}
    assert cli.main(["send", "master", "hi"], environ) == 0
    assert posted == {"folder": tmp_path, "sender": "eng-1", "to": "master", "text": "hi", "now": 1700000000}
    assert created["environ"] == dict(environ)
    assert capsys.readouterr().out == "posted to master: ok\n"


def test_stop_tolerates_an_already_missing_registry_file_and_room(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "quick-abc"
    folder.mkdir()
    master = registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master")
    swarm = registry.Swarm("quick-abc", "/work", (master,))
    found = member.Member(folder, swarm, master)
    monkeypatch.setattr(cli, "_member", lambda environ: found)
    assert not registry.path(folder).exists()
    environ = {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / ".agentihooks")}
    assert cli.main(["stop"], environ) == 0
    assert capsys.readouterr().out == f"stopped quick swarm quick-abc; seat folders stay in {folder}\n"


def test_tick_runs_one_round_and_prints_its_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, object] = {}

    def run_round(seen_environ: dict[str, str]) -> list[dict[str, str]]:
        calls["environ"] = seen_environ
        return [{"swarm": "quick-abc"}]

    monkeypatch.setattr(cli.tick, "run_round", run_round)
    environ = {"HOME": str(tmp_path)}
    assert cli.main(["tick"], environ) == 0
    assert calls["environ"] == environ
    assert capsys.readouterr().out == json.dumps([{"swarm": "quick-abc"}]) + "\n"


def test_by_variables_returns_none_without_a_crash_when_only_the_seat_is_set() -> None:
    environ = {member.SEAT_ENV: "eng-1"}
    assert member.find(environ) is None


def test_by_variables_returns_none_when_the_named_seat_is_not_in_the_swarm(tmp_path: Path) -> None:
    master = registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master")
    swarm = registry.Swarm("quick-abc", "/work", (master,))
    registry.save(tmp_path, swarm)
    environ = {member.DIR_ENV: str(tmp_path), member.SEAT_ENV: "eng-9"}
    assert member.find(environ) is None
