from pathlib import Path

import pytest

from scripts.quick import paths, registry, room, seeds
from scripts.quick.layout import MASTER
from tests.quick.test_seeds import _member

pytestmark = pytest.mark.unit


def test_reach_prefers_sendmessage_only_between_two_claude_seats_other_than_master() -> None:
    sender = registry.Seat(2, "eng-1", "w1:p2", "claude", "quick-abc-eng-1")
    receiver = registry.Seat(3, "eng-2", "w1:p3", "claude", "quick-abc-eng-2")
    assert seeds._reach(sender, receiver) == "SendMessage to quick-abc-eng-2"


def test_the_master_seed_pins_every_exact_sentence(tmp_path: Path) -> None:
    found = _member(tmp_path, "master")
    expected = "\n".join(
        [
            "You are the master of quick swarm quick-abc, in herdr pane 1. You are the operator's "
            "front door: the operator talks only to you.",
            "Take work from whatever source the operator names, tickets, issues or pull requests, with the tools you have.",
            "Your engineers, the only agents you contact:",
            f"- eng-1 in pane 2 (claude, session quick-abc-eng-1); seat folder {paths.seat_dir(tmp_path, 'eng-1')}",
            f"- eng-2 in pane 3 (codex, session quick-abc-eng-2); seat folder {paths.seat_dir(tmp_path, 'eng-2')}",
            "At the start tell each engineer to wait for the operator's instructions. The operator briefs each "
            "engineer directly; the engineer writes its plan to its seat folder and reports it to you before it "
            "starts. Engineers own their plans: assign work only when the operator asks you to.",
            "Track each engineer through its seat folder: plan.md holds its checkbox stages, notes.md its notes, "
            "learned.md its lessons. Tell the operator what each engineer is doing and where its notes are.",
            "Reach a Claude engineer with SendMessage to its session; reach any other seat with "
            '`agentihooks quick send <seat> "<text>"` and read what seats sent you with `agentihooks quick room`.',
            seeds.HANDOFF.format(handoff=paths.handoff(tmp_path, MASTER), learned=paths.learned(tmp_path, MASTER)),
        ]
    )
    assert seeds.opening(found) == expected


def test_an_engineer_seed_pins_every_exact_sentence_for_a_claude_seat(tmp_path: Path) -> None:
    found = _member(tmp_path, "eng-1")
    expected = "\n".join(
        [
            "You are eng-1 of quick swarm quick-abc, in herdr pane 2. The master "
            "in pane 1 is the sole authority and the operator's front door.",
            "Wait for the operator's instructions. Talk to the operator only while planning your task.",
            f"When briefed, write your plan to {paths.plan(tmp_path, 'eng-1')} as checkbox stages "
            "(- [ ] stage), then report the plan to the master before you start.",
            "Work one plan at a time. Tick each stage as it lands, keep notes in notes.md and lessons in learned.md "
            f"in {paths.seat_dir(tmp_path, 'eng-1')}.",
            'Report progress, blockers and the finished plan to the master: `agentihooks quick send master "<text>"`. '
            "Read what other seats sent you with `agentihooks quick room`.",
            "When the plan is complete, report it to the master and wait for the next instruction.",
            seeds.HANDOFF.format(handoff=paths.handoff(tmp_path, "eng-1"), learned=paths.learned(tmp_path, "eng-1")),
        ]
    )
    assert seeds.opening(found) == expected


def test_an_engineer_seed_pins_every_exact_sentence_for_a_codex_seat(tmp_path: Path) -> None:
    found = _member(tmp_path, "eng-2")
    expected = "\n".join(
        [
            "You are eng-2 of quick swarm quick-abc, in herdr pane 3. The master "
            "in pane 1 is the sole authority and the operator's front door.",
            "Wait for the operator's instructions. Talk to the operator only while planning your task.",
            f"When briefed, write your plan to {paths.plan(tmp_path, 'eng-2')} as checkbox stages "
            "(- [ ] stage), then report the plan to the master before you start.",
            "Work one plan at a time. Tick each stage as it lands, keep notes in notes.md and lessons in learned.md "
            f"in {paths.seat_dir(tmp_path, 'eng-2')}.",
            'Report progress, blockers and the finished plan to the master: `agentihooks quick send master "<text>"`. '
            "Read what other seats sent you with `agentihooks quick room`.",
            "When the plan is complete, report it to the master and wait for the next instruction.",
            seeds.HANDOFF.format(handoff=paths.handoff(tmp_path, "eng-2"), learned=paths.learned(tmp_path, "eng-2")),
        ]
    )
    assert seeds.opening(found) == expected


def test_block_pins_the_role_and_the_no_plan_default_for_the_master(tmp_path: Path) -> None:
    found = _member(tmp_path, "master")
    room.post(tmp_path, "eng-1", "master", "plan ready", 1)
    paths.culture(tmp_path).write_text("Ship small.\n")
    expected = (
        "QUICK SWARM — you are master of quick swarm quick-abc, herdr pane 1 (master: the operator's front door). "
        f"Seat folder {paths.seat_dir(tmp_path, 'master')}. Plan: no plan yet. "
        "Room: 1 message(s) for you; read them with `agentihooks quick room`.\n"
        "Culture: Ship small."
    )
    assert seeds.block(found) == expected


def test_block_pins_the_role_and_the_next_none_default_for_an_engineer(tmp_path: Path) -> None:
    found = _member(tmp_path, "eng-1")
    paths.plan(tmp_path, "eng-1").write_text("- [x] build\n- [x] ship\n")
    expected = (
        "QUICK SWARM — you are eng-1 of quick swarm quick-abc, herdr pane 2 (engineer: follow your plan). "
        f"Seat folder {paths.seat_dir(tmp_path, 'eng-1')}. Plan: 2 of 2 stages done; next: none."
    )
    assert seeds.block(found) == expected
