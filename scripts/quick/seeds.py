"""The role seeds a quick swarm seat opens with and the short block injected into it on cadence."""

import re

from scripts.quick import paths, room
from scripts.quick.layout import MASTER
from scripts.quick.member import Member
from scripts.quick.registry import Seat

MAX_CHARS = 1500
CHECKBOX = re.compile(r"^[ \t]*[-*][ \t]+\[([ xX])\](.*)$", re.MULTILINE)
SEND = 'agentihooks quick send {to} "<text>"'
HANDOFF = (
    "Near your context limit write your Handoff v2 to {handoff}, put any lesson for the seat in {learned}, "
    "then run `agentihooks quick handoff`; your successor starts in this same pane from it."
)


def plan_state(text: str) -> tuple[int, int, str]:
    """(stages done, stages in total, the first open stage) of a checkbox plan."""
    stages = CHECKBOX.findall(text)
    done = sum(1 for mark, _ in stages if mark.lower() == "x")
    upcoming = next((title.strip() for mark, title in stages if mark == " "), "")
    return done, len(stages), upcoming


def _reach(sender: Seat, receiver: Seat) -> str:
    if receiver.seat != MASTER and sender.harness == receiver.harness == "claude":
        return f"SendMessage to {receiver.name}"
    return f"`{SEND.format(to=receiver.seat)}`"


def _engineer_line(found: Member, seat: Seat) -> str:
    folder = paths.seat_dir(found.folder, seat.seat)
    return f"- {seat.seat} in pane {seat.position} ({seat.harness}, session {seat.name}); seat folder {folder}"


def _master(found: Member) -> str:
    engineers = [seat for seat in found.swarm.seats if seat.seat != MASTER]
    lines = [
        f"You are the master of quick swarm {found.swarm.quick_id}, in herdr pane 1. You are the operator's "
        "front door: the operator talks only to you.",
        "Take work from whatever source the operator names, tickets, issues or pull requests, with the tools you have.",
        "Your engineers, the only agents you contact:",
        *(_engineer_line(found, seat) for seat in engineers),
        "At the start tell each engineer to wait for the operator's instructions. The operator briefs each "
        "engineer directly; the engineer writes its plan to its seat folder and reports it to you before it "
        "starts. Engineers own their plans: assign work only when the operator asks you to.",
        "Track each engineer through its seat folder: plan.md holds its checkbox stages, notes.md its notes, "
        "learned.md its lessons. Tell the operator what each engineer is doing and where its notes are.",
        "Reach a Claude engineer with SendMessage to its session; reach any other seat with "
        '`agentihooks quick send <seat> "<text>"` and read what seats sent you with `agentihooks quick room`.',
        HANDOFF.format(handoff=paths.handoff(found.folder, MASTER), learned=paths.learned(found.folder, MASTER)),
    ]
    return "\n".join(lines)


def _engineer(found: Member) -> str:
    seat = found.seat
    master = found.swarm.seat(MASTER) or seat
    lines = [
        f"You are {seat.seat} of quick swarm {found.swarm.quick_id}, in herdr pane {seat.position}. The master "
        f"in pane 1 is the sole authority and the operator's front door.",
        "Wait for the operator's instructions. Talk to the operator only while planning your task.",
        f"When briefed, write your plan to {paths.plan(found.folder, seat.seat)} as checkbox stages "
        "(- [ ] stage), then report the plan to the master before you start.",
        "Work one plan at a time. Tick each stage as it lands, keep notes in notes.md and lessons in learned.md "
        f"in {paths.seat_dir(found.folder, seat.seat)}.",
        f"Report progress, blockers and the finished plan to the master: {_reach(seat, master)}. Read what other "
        "seats sent you with `agentihooks quick room`.",
        "When the plan is complete, report it to the master and wait for the next instruction.",
        HANDOFF.format(handoff=paths.handoff(found.folder, seat.seat), learned=paths.learned(found.folder, seat.seat)),
    ]
    return "\n".join(lines)


def opening(found: Member) -> str:
    """The role seed this seat's agent opens with."""
    return _master(found) if found.seat.seat == MASTER else _engineer(found)


def block(found: Member) -> str:
    """The short quick swarm block injected into the seat on cadence, at most MAX_CHARS long."""
    seat = found.seat
    done, total, upcoming = plan_state(paths.read_text(paths.plan(found.folder, seat.seat)))
    progress = f"{done} of {total} stages done; next: {upcoming or 'none'}" if total else "no plan yet"
    role = "master: the operator's front door" if seat.seat == MASTER else "engineer: follow your plan"
    head = (
        f"QUICK SWARM — you are {seat.seat} of quick swarm {found.swarm.quick_id}, herdr pane {seat.position} "
        f"({role}). Seat folder {paths.seat_dir(found.folder, seat.seat)}. Plan: {progress}."
    )
    waiting = room.waiting(found.folder, seat.seat)
    head += f" Room: {waiting} message(s) for you; read them with `agentihooks quick room`." if waiting else ""
    culture = paths.read_text(paths.culture(found.folder)).strip()
    text = f"{head}\nCulture: {culture}" if culture else head
    return text[:MAX_CHARS]
