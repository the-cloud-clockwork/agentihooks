from pathlib import Path

import pytest

from scripts.quick import launch, paths, registry

pytestmark = pytest.mark.unit


def _swarm(folder: Path, eng_state: str) -> None:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master"),
        registry.Seat(2, "eng-1", "w1:p2", "codex", "quick-abc-eng-1", state=eng_state),
    )
    registry.save(folder, registry.Swarm("quick-abc", "/work", seats))
    paths.create(folder, ["master", "eng-1"])


def test_relaunch_of_a_seat_that_is_not_pending_starts_nothing(tmp_path: Path) -> None:
    _swarm(tmp_path, registry.READY)
    started: list[list[str]] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append(argv)
        return 0

    outcome = launch.relaunch(tmp_path, "eng-1", {}, bare=lambda pane: True, start=start, sleep=lambda s: None)
    loaded = registry.load(tmp_path)
    assert outcome == "not-pending" and started == []
    assert loaded is not None and loaded.seats[1].state == registry.READY


def test_relaunch_starts_at_once_when_the_pane_is_already_bare(tmp_path: Path) -> None:
    _swarm(tmp_path, registry.PENDING)
    started: list[list[str]] = []

    def start(argv: list[str], environ: dict[str, str]) -> int:
        started.append(argv)
        return 0

    outcome = launch.relaunch(
        tmp_path, "eng-1", {}, bare=lambda pane: True, start=start, wait_s=0.0, sleep=lambda s: None
    )
    assert outcome == "exited" and len(started) == 1
