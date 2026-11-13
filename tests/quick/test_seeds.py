from pathlib import Path

import pytest

from scripts.quick import member, paths, registry, room, seeds

pytestmark = pytest.mark.unit


def _member(folder: Path, seat: str) -> member.Member:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master", session_id="s-master"),
        registry.Seat(2, "eng-1", "w1:p3", "claude", "quick-abc-eng-1"),
        registry.Seat(3, "eng-2", "w1:p2", "codex", "quick-abc-eng-2"),
    )
    swarm = registry.Swarm("quick-abc", "/work", seats)
    paths.create(folder, [found.seat for found in seats])
    found = swarm.seat(seat)
    assert found is not None
    return member.Member(folder, swarm, found)


@pytest.mark.feature("quick-swarm")
def test_the_master_seed_makes_it_the_front_door_over_its_own_panes(tmp_path):
    text = seeds.opening(_member(tmp_path, "master"))
    assert "front door" in text and "quick-abc" in text
    assert "eng-1 in pane 2 (claude, session quick-abc-eng-1)" in text
    assert "eng-2 in pane 3 (codex, session quick-abc-eng-2)" in text
    assert "wait for the operator" in text and "only" in text
    assert "SendMessage" in text and "agentihooks quick send <seat>" in text and "agentihooks quick room" in text
    assert "Reach a Claude engineer with SendMessage to its session" in text and "session quick-abc-eng-1" in text
    assert str(paths.seat_dir(tmp_path, "eng-1")) in text
    assert "agentihooks quick handoff" in text and str(paths.handoff(tmp_path, "master")) in text


@pytest.mark.feature("quick-swarm")
def test_an_engineer_seed_plans_first_and_reports_to_the_master_before_starting(tmp_path):
    text = seeds.opening(_member(tmp_path, "eng-1"))
    assert "eng-1 of quick swarm quick-abc, in herdr pane 2" in text
    assert "Wait for the operator" in text
    assert str(paths.plan(tmp_path, "eng-1")) in text and "- [ ]" in text
    assert "report the plan to the master before you start" in text
    assert "one plan at a time" in text
    assert 'agentihooks quick send master "<text>"' in text and "SendMessage to quick-abc-master" not in text
    assert str(paths.handoff(tmp_path, "eng-1")) in text and "agentihooks quick handoff" in text


def test_a_codex_seat_reaches_the_master_through_the_room(tmp_path):
    text = seeds.opening(_member(tmp_path, "eng-2"))
    assert 'agentihooks quick send master "<text>"' in text and "SendMessage" not in text


def test_a_claude_engineer_reaches_a_codex_master_through_the_room(tmp_path):
    found = _member(tmp_path, "eng-1")
    master = registry.Seat(1, "master", "w1:p1", "codex", "quick-abc-master")
    swarm = registry.Swarm("quick-abc", "/work", (master, *found.swarm.seats[1:]))
    text = seeds.opening(member.Member(tmp_path, swarm, found.seat))
    assert 'agentihooks quick send master "<text>"' in text and "SendMessage to" not in text


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("", (0, 0, "")),
        ("- [x] build\n- [ ] test it\n- [ ] ship\n", (1, 3, "test it")),
        ("- [X] build\n* [x] test\n", (2, 2, "")),
        ("notes only\n", (0, 0, "")),
        ("  - [ ]   indented stage  \n-[ ] no space\n", (0, 1, "indented stage")),
        ("-" + " " * 5000 + "[\n", (0, 0, "")),
    ],
)
def test_plan_state_counts_checkbox_stages(plan, expected):
    assert seeds.plan_state(plan) == expected


@pytest.mark.feature("quick-swarm")
def test_the_injected_block_names_the_seat_its_folder_and_its_next_stage(tmp_path):
    found = _member(tmp_path, "eng-1")
    paths.plan(tmp_path, "eng-1").write_text("- [x] build\n- [ ] test it\n- [ ] ship\n")
    paths.culture(tmp_path).write_text("Ship small.\n")
    text = seeds.block(found)
    assert text.startswith("QUICK SWARM")
    assert "eng-1 of quick swarm quick-abc, herdr pane 2" in text
    assert str(paths.seat_dir(tmp_path, "eng-1")) in text
    assert "1 of 3 stages done; next: test it" in text and "Ship small." in text
    assert len(text) <= seeds.MAX_CHARS


@pytest.mark.feature("quick-swarm")
def test_the_block_tells_a_seat_of_room_lines_waiting_for_it(tmp_path):
    found = _member(tmp_path, "master")
    assert "Room:" not in seeds.block(found)
    room.post(tmp_path, "eng-1", "master", "plan ready", 1)
    assert "Room: 1 message(s) for you; read them with `agentihooks quick room`." in seeds.block(found)


def test_the_block_without_a_plan_says_so_and_stays_within_its_cap(tmp_path):
    found = _member(tmp_path, "master")
    paths.culture(tmp_path).write_text("x" * 5000)
    text = seeds.block(found)
    assert "no plan yet" in text and len(text) <= seeds.MAX_CHARS
