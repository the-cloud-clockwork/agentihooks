"""A quick seat stays bound to its live session: a relaunched agent records its pane and session in the seat, a
launch whose agent never records itself leaves the seat pending, and quick relaunch --adopt seats a live session."""

import argparse
from pathlib import Path

import pytest

import hooks.context.context_recycle as recycle
from hooks.context import quick_swarm
from hooks.lifecycle import handoff_close
from scripts.quick import cli, launch, member, paths, registry, tick

pytestmark = pytest.mark.unit

QUICK = "quick-e47e73f0"
OLD = "6d0c893d-1111-4111-8111-111111111111"
NEW = "8414a54e-2222-4222-8222-222222222222"
HANDOFF = (
    "# Handoff v2\n## Intent\nx\n## Done\nNone\n## Stopped at\nx\n## Decisions and promises\nNone\n"
    "## Next\nx\n## Read first\nNone\n<!-- handoff complete -->\n"
)


class Herdr:
    def __init__(self, bare: dict[str, bool | None]) -> None:
        self.panes = bare
        self.prompts: list[tuple[str, str]] = []

    def status(self, pane_id: str) -> str:
        return "idle"

    def typed(self, pane_id: str) -> str:
        return ""

    def bare(self, pane_id: str) -> bool | None:
        return self.panes.get(pane_id)

    def screen(self, pane_id: str) -> str:
        return ""

    def prompt(self, pane_id: str, text: str) -> bool:
        self.prompts.append((pane_id, text))
        return True


@pytest.fixture(autouse=True)
def _recycle_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path / "recycle")
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: None)
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: None)


def _home(tmp_path: Path) -> dict[str, str]:
    return {"AGENTIHOOKS_HOME": str(tmp_path / "home")}


def _swarm(tmp_path: Path, master_state: str = registry.READY) -> Path:
    folder = tmp_path / QUICK
    seats = (
        registry.Seat(1, "master", "w1:p8", "claude", f"{QUICK}-master", session_id=OLD, state=master_state),
        registry.Seat(2, "eng-1", "w1:p2", "claude", f"{QUICK}-eng-1"),
    )
    registry.save(folder, registry.Swarm(QUICK, "/work", seats))
    paths.create(folder, ["master", "eng-1"])
    paths.register(_home(tmp_path), QUICK, folder)
    return folder


def _unbound(tmp_path: Path) -> dict[str, str]:
    return {**_home(tmp_path), "HERDR_PANE_ID": "w1:pA", "CLAUDE_CODE_SESSION_ID": NEW}


def _master(folder: Path) -> registry.Seat:
    swarm = registry.load(folder)
    seat = swarm.seat("master") if swarm else None
    assert seat is not None
    return seat


def _relaunch_into_a_new_pane(tmp_path: Path) -> tuple[Path, str]:
    folder = _swarm(tmp_path, master_state=registry.PENDING)
    started: list[dict[str, str]] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append(environ)
        quick_swarm.refocus(NEW, "prompt", {**environ, "HERDR_PANE_ID": "w1:pA"})
        return 0

    def bare(pane: str) -> bool:
        return not started

    outcome = launch.relaunch(
        folder, "master", _home(tmp_path), bare=bare, start=start, wait_s=0.0, sleep=lambda s: None
    )
    assert started and started[0][member.SEAT_ENV] == "master"
    return folder, outcome


@pytest.mark.feature("quick-seat-binding")
def test_a_relaunched_master_in_a_new_pane_records_it_and_stays_a_member_without_its_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, outcome = _relaunch_into_a_new_pane(tmp_path)
    seat = _master(folder)
    assert outcome == "started"
    assert (seat.pane_id, seat.session_id, seat.state) == ("w1:pA", NEW, registry.READY)
    unbound = _unbound(tmp_path)
    assert cli.main(["status"], unbound) == 0
    herdr = Herdr({"w1:pA": False})
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: herdr)
    engineer = {**_home(tmp_path), member.DIR_ENV: str(folder), member.SEAT_ENV: "eng-1", "HERDR_PANE_ID": "w1:p2"}
    capsys.readouterr()
    assert cli.main(["send", "master", "plan ready"], engineer) == 0
    assert capsys.readouterr().out == "posted to master: room\n"
    assert herdr.prompts == [("w1:pA", tick.ROOM.format(count=1))]
    paths.handoff(folder, "master").write_text(HANDOFF)
    assert cli.main(["handoff"], unbound) == 0
    assert "starts in w1:pA" in capsys.readouterr().out
    pending = handoff_close.quick_pending(unbound)
    assert pending is not None and pending.seat.seat == "master" and pending.folder == folder


@pytest.mark.feature("quick-seat-binding")
def test_a_relaunched_seat_whose_session_lacks_its_variables_is_told_to_hand_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder, _ = _relaunch_into_a_new_pane(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 610_000 if session_id == NEW else None)
    text = recycle.directive(NEW, _unbound(tmp_path))
    assert text is not None and text.startswith("HANDOFF PREPARATION")
    assert str(paths.handoff(folder, "master")) in text and "agentihooks quick handoff" in text


@pytest.mark.feature("quick-seat-binding")
def test_only_a_launching_seat_named_by_the_session_variables_records_its_pane_and_session(tmp_path: Path) -> None:
    folder = _swarm(tmp_path, master_state=registry.LAUNCHING)
    seated = {**_home(tmp_path), member.DIR_ENV: str(folder), member.SEAT_ENV: "master"}
    assert quick_swarm.refocus(NEW, "prompt", seated) != ""
    assert (_master(folder).pane_id, _master(folder).session_id) == ("w1:p8", OLD)
    assert quick_swarm.refocus("", "prompt", {**seated, "HERDR_PANE_ID": "w1:pA"}) == ""
    assert (_master(folder).pane_id, _master(folder).session_id) == ("w1:p8", OLD)
    quick_swarm.refocus(NEW, "prompt", {**_home(tmp_path), "HERDR_PANE_ID": "w1:p8"})
    assert _master(folder).session_id == OLD
    quick_swarm.refocus(NEW, "prompt", {**seated, "HERDR_PANE_ID": "w1:pA"})
    assert (_master(folder).pane_id, _master(folder).session_id, _master(folder).state) == (
        "w1:pA",
        NEW,
        registry.LAUNCHING,
    )
    registry.set_state(folder, "master", registry.READY)
    quick_swarm.refocus("9999aaaa-3333-4333-8333-333333333333", "tool", {**seated, "HERDR_PANE_ID": "w1:pB"})
    assert (_master(folder).pane_id, _master(folder).session_id) == ("w1:pA", NEW)


@pytest.mark.feature("quick-seat-binding")
def test_a_relaunch_whose_agent_never_records_its_session_leaves_the_seat_pending(tmp_path: Path) -> None:
    folder = _swarm(tmp_path, master_state=registry.PENDING)
    started: list[list[str]] = []
    naps: list[float] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append(argv)
        return 0

    def bare(pane: str) -> bool:
        return not started

    outcome = launch.relaunch(folder, "master", _home(tmp_path), bare=bare, start=start, wait_s=0.0, sleep=naps.append)
    seat = _master(folder)
    assert outcome == "unbound" and len(started) == 1
    assert (seat.pane_id, seat.session_id, seat.state) == ("w1:p8", OLD, registry.PENDING)
    assert sum(naps) == launch.SETTLE_S + launch.BIND_S == 110.0


@pytest.mark.feature("quick-seat-binding")
def test_a_relaunch_whose_agent_exits_never_waits_for_its_session(tmp_path: Path) -> None:
    folder = _swarm(tmp_path, master_state=registry.PENDING)
    naps: list[float] = []
    outcome = launch.relaunch(
        folder, "master", _home(tmp_path), bare=lambda pane: True, start=lambda a, e: 0, wait_s=0.0, sleep=naps.append
    )
    assert outcome == "exited" and naps == [launch.POLL_S]
    assert _master(folder).state == registry.PENDING


@pytest.mark.feature("quick-seat-binding")
def test_an_unbound_relaunch_fails_the_command_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: Herdr({}))
    monkeypatch.setattr(cli.launch, "relaunch", lambda folder, seat_name, base, *, bare: "unbound")
    assert cli.main(["relaunch", "master", "--folder", str(tmp_path)], _home(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == "unbound\n"
    assert captured.err == (
        "agentihooks quick: master started but its agent never recorded its session in the seat, so the seat stays "
        f"pending; from the pane it runs in, run agentihooks quick relaunch master --folder {tmp_path} --adopt\n"
    )


@pytest.mark.feature("quick-seat-binding")
def test_adopt_seats_a_live_session_started_outside_the_swarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _swarm(tmp_path)
    unbound = _unbound(tmp_path)
    seen: list[dict[str, str]] = []

    def live(environ: dict[str, str]) -> Herdr:
        seen.append(environ)
        return Herdr({"w1:p8": None})

    monkeypatch.setattr(cli.guard, "require", lambda environ: environ["HERDR_PANE_ID"])
    monkeypatch.setattr(cli.tick, "LiveHerdr", live)
    assert cli.main(["status"], unbound) == 2
    capsys.readouterr()
    assert cli.main(["relaunch", "master", "--folder", str(folder), "--adopt"], unbound) == 0
    assert capsys.readouterr().out == "adopted\n" and seen == [unbound]
    seat = _master(folder)
    assert (seat.pane_id, seat.session_id, seat.state) == ("w1:pA", NEW, registry.READY)
    assert cli.main(["status"], unbound) == 0
    paths.handoff(folder, "master").write_text(HANDOFF)
    assert cli.main(["handoff"], unbound) == 0
    assert _master(folder).state == registry.PENDING


@pytest.mark.feature("quick-seat-binding")
def test_a_live_session_in_the_seats_own_pane_adopts_it_to_record_its_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _swarm(tmp_path, master_state=registry.PENDING)
    monkeypatch.setattr(cli.guard, "require", lambda environ: "w1:p8")
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: Herdr({"w1:p8": False}))
    here = {**_home(tmp_path), "HERDR_PANE_ID": "w1:p8", "CLAUDE_CODE_SESSION_ID": NEW}
    assert cli.main(["relaunch", "master", "--folder", str(folder), "--adopt"], here) == 0
    assert capsys.readouterr().out == "adopted\n"
    assert (_master(folder).pane_id, _master(folder).session_id, _master(folder).state) == (
        "w1:p8",
        NEW,
        registry.READY,
    )


@pytest.mark.feature("quick-seat-binding")
@pytest.mark.parametrize(
    ("state", "pane", "old_pane", "outcome"),
    [
        (registry.LAUNCHING, "w1:pA", None, "launching"),
        (registry.READY, "w1:pA", False, "occupied"),
        (registry.READY, "w1:p2", None, "pane-of-eng-1"),
    ],
)
def test_adopt_never_takes_a_launching_seat_a_seat_whose_agent_runs_or_another_seats_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
    pane: str,
    old_pane: bool | None,
    outcome: str,
) -> None:
    folder = _swarm(tmp_path, master_state=state)
    before = registry.load(folder)
    monkeypatch.setattr(cli.guard, "require", lambda environ: pane)
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: Herdr({"w1:p8": old_pane}))
    assert cli.main(["relaunch", "master", "--folder", str(folder), "--adopt"], _unbound(tmp_path)) == 1
    assert capsys.readouterr().out == f"{outcome}\n"
    assert registry.load(folder) == before


@pytest.mark.feature("quick-seat-binding")
def test_adopt_of_a_seat_the_swarm_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _swarm(tmp_path)
    monkeypatch.setattr(cli.guard, "require", lambda environ: "w1:pA")
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: Herdr({}))
    assert cli.main(["relaunch", "eng-9", "--folder", str(folder), "--adopt"], _unbound(tmp_path)) == 1
    assert capsys.readouterr().out == "no-seat\n"
    assert cli.main(["relaunch", "master", "--folder", str(tmp_path / "gone"), "--adopt"], _unbound(tmp_path)) == 1
    assert capsys.readouterr().out == "no-seat\n"


@pytest.mark.feature("quick-seat-binding")
def test_adopt_with_no_session_id_seats_the_pane_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _swarm(tmp_path)
    monkeypatch.setattr(cli.guard, "require", lambda environ: "w1:pA")
    monkeypatch.setattr(cli.tick, "LiveHerdr", lambda environ: Herdr({}))
    assert cli.main(["relaunch", "master", "--folder", str(folder), "--adopt"], _home(tmp_path)) == 0
    assert (_master(folder).pane_id, _master(folder).session_id) == ("w1:pA", "")
    with pytest.raises(SystemExit):
        cli.main(["relaunch", "master", "--folder", str(folder), "--adopt", "--force"], _home(tmp_path))
    again = cli.parser().parse_args(["relaunch", "master", "--folder", str(folder)])
    assert (again.adopt, again.force) == (False, False)
    sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
    option = sub.choices["relaunch"]._option_string_actions["--adopt"]
    assert option.help == "Record this live session and its herdr pane as the seat's occupant"


@pytest.mark.feature("quick-seat-binding")
def test_bind_needs_the_seat_variables_a_pane_and_a_session_and_returns_the_recorded_seat(tmp_path: Path) -> None:
    folder = _swarm(tmp_path, master_state=registry.LAUNCHING)
    seated = {**_home(tmp_path), member.DIR_ENV: str(folder), member.SEAT_ENV: "master", "HERDR_PANE_ID": "w1:pA"}
    assert member.bind(seated, "") is None
    assert member.bind({**seated, "HERDR_PANE_ID": ""}, NEW) is None
    assert member.bind({**_home(tmp_path), "HERDR_PANE_ID": "w1:p8"}, NEW) is None
    assert (_master(folder).pane_id, _master(folder).session_id) == ("w1:p8", OLD)
    found = member.bind(seated, NEW)
    assert found is not None and found.folder == folder
    assert (found.seat.seat, found.seat.pane_id, found.seat.session_id) == ("master", "w1:pA", NEW)
    assert found.swarm.seat("master") == found.seat == _master(folder)


@pytest.mark.feature("quick-seat-binding")
def test_the_bind_wait_polls_each_interval_and_needs_a_session_other_than_the_predecessors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = _swarm(tmp_path, master_state=registry.LAUNCHING)
    before = _master(folder)
    monkeypatch.setattr(launch, "POLL_S", 0.5)
    naps: list[float] = []
    assert launch.bound(folder, before, wait_s=2.0, sleep=naps.append) is False
    assert naps == [0.5] * 4
    registry.bind(folder, "master", "w1:p8", OLD)
    assert launch.bound(folder, before, wait_s=0.0, sleep=naps.append) is False
    registry.bind(folder, "master", "w1:p8", "")
    assert launch.bound(folder, before, wait_s=0.0, sleep=naps.append) is False
    assert launch.bound(tmp_path / "gone", before, wait_s=0.0, sleep=naps.append) is False

    def later(seconds: float) -> None:
        naps.append(seconds)
        registry.bind(folder, "master", "w1:p8", NEW)

    naps.clear()
    assert launch.bound(folder, before, wait_s=2.0, sleep=later) is True
    assert naps == [0.5]
    registry.bind(folder, "master", "w1:p8", OLD)
    naps.clear()
    assert launch.bound(folder, before, wait_s=0.5, sleep=later) is True
    assert naps == [0.5]
