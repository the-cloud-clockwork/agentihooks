import json
from pathlib import Path

import pytest

from scripts.quick import member, paths, registry

pytestmark = pytest.mark.unit


def _swarm(folder: Path) -> registry.Swarm:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master", session_id="s-master"),
        registry.Seat(2, "eng-1", "w1:p3", "claude", "quick-abc-eng-1"),
        registry.Seat(3, "eng-2", "w1:p2", "codex", "quick-abc-eng-2"),
    )
    found = registry.Swarm("quick-abc", "/work", seats)
    registry.save(folder, found)
    return found


@pytest.mark.feature("quick-swarm")
def test_the_registry_round_trips_through_its_file(tmp_path):
    saved = _swarm(tmp_path)
    assert registry.load(tmp_path) == saved
    data = json.loads(registry.path(tmp_path).read_text())
    assert data["quick_id"] == "quick-abc" and [seat["seat"] for seat in data["seats"]] == ["master", "eng-1", "eng-2"]


def test_a_missing_or_broken_registry_loads_as_none(tmp_path):
    assert registry.load(tmp_path) is None
    registry.path(tmp_path).write_text("{not json")
    assert registry.load(tmp_path) is None


def test_update_changes_one_seat_and_keeps_the_rest(tmp_path):
    _swarm(tmp_path)
    changed = registry.set_state(tmp_path, "eng-1", registry.PENDING)
    assert changed is not None and changed.state == registry.PENDING
    loaded = registry.load(tmp_path)
    assert loaded is not None
    assert [seat.state for seat in loaded.seats] == [registry.READY, registry.PENDING, registry.READY]
    assert registry.set_state(tmp_path, "eng-9", registry.PENDING) is None


@pytest.mark.feature("quick-swarm")
def test_a_pending_seat_is_taken_for_launch_only_once(tmp_path):
    _swarm(tmp_path)
    registry.set_state(tmp_path, "eng-1", registry.PENDING)
    first = registry.take(tmp_path, "eng-1", registry.PENDING, registry.LAUNCHING)
    second = registry.take(tmp_path, "eng-1", registry.PENDING, registry.LAUNCHING)
    assert first is not None and first.state == registry.LAUNCHING
    assert second is None


def test_a_seat_is_found_by_its_pane(tmp_path):
    _swarm(tmp_path)
    loaded = registry.load(tmp_path)
    assert loaded is not None
    assert loaded.at_pane("w1:p2") == loaded.seat("eng-2")
    assert loaded.at_pane("w9:p9") is None and loaded.seat("eng-9") is None


def test_create_makes_every_seat_folder_without_overwriting(tmp_path):
    paths.create(tmp_path, ["master", "eng-1"])
    paths.plan(tmp_path, "eng-1").write_text("- [x] one\n")
    paths.create(tmp_path, ["master", "eng-1"])
    assert paths.plan(tmp_path, "eng-1").read_text() == "- [x] one\n"
    for seat in ("master", "eng-1"):
        assert all((paths.seat_dir(tmp_path, seat) / name).is_file() for name in paths.SEAT_FILES)
    assert paths.room(tmp_path).parent.is_dir() and paths.culture(tmp_path).is_file()


def test_the_index_registers_and_forgets_quick_swarms(tmp_path):
    environ = {"AGENTIHOOKS_HOME": str(tmp_path / "home")}
    paths.register(environ, "quick-abc", tmp_path / "a")
    paths.register(environ, "quick-def", tmp_path / "b")
    assert paths.folders(environ) == [tmp_path / "a", tmp_path / "b"]
    paths.unregister(environ, "quick-abc")
    assert paths.folders(environ) == [tmp_path / "b"]


@pytest.mark.feature("quick-swarm")
def test_a_launched_seat_is_a_member_by_its_variables(tmp_path):
    _swarm(tmp_path)
    environ = {member.DIR_ENV: str(tmp_path), member.SEAT_ENV: "eng-2", "AGENTIHOOKS_HOME": str(tmp_path / "home")}
    found = member.find(environ)
    assert found is not None and found.seat.seat == "eng-2" and found.folder == tmp_path


@pytest.mark.feature("quick-swarm")
def test_the_original_master_is_a_member_by_its_herdr_pane(tmp_path):
    _swarm(tmp_path)
    home = tmp_path / "home"
    paths.register({"AGENTIHOOKS_HOME": str(home)}, "quick-abc", tmp_path)
    found = member.find({"HERDR_PANE_ID": "w1:p1", "AGENTIHOOKS_HOME": str(home)})
    assert found is not None and found.seat.seat == "master"
    assert member.find({"HERDR_PANE_ID": "w7:p7", "AGENTIHOOKS_HOME": str(home)}) is None


def test_a_stopped_quick_swarm_has_no_members(tmp_path):
    environ = {member.DIR_ENV: str(tmp_path), member.SEAT_ENV: "eng-1", "AGENTIHOOKS_HOME": str(tmp_path / "h")}
    assert member.find(environ) is None
    assert member.find({"AGENTIHOOKS_HOME": str(tmp_path / "h")}) is None
