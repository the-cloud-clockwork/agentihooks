"""Launch a quick seat's agent into its own herdr pane, first at quick up and again after each handoff."""

import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from scripts.quick import paths, registry, seeds
from scripts.quick.member import DIR_ENV, QUICK_ENV, SEAT_ENV, Member

WAIT_BARE_S = 60.0
SETTLE_S = 20.0
POLL_S = 1.0
SWARM_KEYS = ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_AGENT_NAME")
Start = Callable[[list[str], dict[str, str]], int]


def argv(swarm: registry.Swarm, seat: registry.Seat, prompt_file: Path) -> list[str]:
    """The init-agent arguments that open the seat's agent in its own herdr pane."""
    return [
        *("--host", "herdr", "--pane", seat.pane_id, "--name", seat.name, "--agent", seat.harness),
        *("--dir", swarm.directory, "--prompt-file", str(prompt_file)),
    ]


def environ(base: Mapping[str, str], folder: Path, swarm: registry.Swarm, seat: registry.Seat) -> dict[str, str]:
    """The launch environment: the caller's, without any swarm identity, with the seat's quick variables."""
    kept = {key: value for key, value in base.items() if key not in SWARM_KEYS}
    return {**kept, QUICK_ENV: swarm.quick_id, SEAT_ENV: seat.seat, DIR_ENV: str(folder)}


def opening(found: Member) -> str:
    """The seat's role seed, followed by its predecessor's handoff when the seat is being taken over."""
    seed = seeds.opening(found)
    if found.seat.state == registry.READY:
        return seed
    handed = paths.read_text(paths.handoff(found.folder, found.seat.seat)).strip()
    return (
        f"{seed}\n\nYour predecessor in this seat handed off. Continue from its handoff:\n\n{handed}"
        if handed
        else seed
    )


def init_agent(arguments: list[str], env: dict[str, str]) -> int:
    """Run agentihooks init-agent with ``arguments`` in ``env`` and return its exit status."""
    return subprocess.run([sys.executable, "-m", "scripts.init_agent", *arguments], env=env, check=False).returncode


def _write_opening(found: Member) -> Path:
    target = paths.opening(found.folder, found.seat.seat)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(opening(found))
    return target


def start_seat(folder: Path, swarm: registry.Swarm, seat: registry.Seat, base: Mapping[str, str], start: Start) -> int:
    """Open the seat's agent in its pane with its opening prompt."""
    prompt_file = _write_opening(Member(folder, swarm, seat))
    return start(argv(swarm, seat, prompt_file), environ(base, folder, swarm, seat))


def start_all(folder: Path, swarm: registry.Swarm, base: Mapping[str, str]) -> dict[str, int]:
    """Open every engineer's agent at once, each in its own pane; the exit status per seat."""
    running: dict[str, subprocess.Popen[bytes]] = {}
    for seat in swarm.seats[1:]:
        prompt_file = _write_opening(Member(folder, swarm, seat))
        command = [sys.executable, "-m", "scripts.init_agent", *argv(swarm, seat, prompt_file)]
        running[seat.seat] = subprocess.Popen(command, env=environ(base, folder, swarm, seat))
    return {name: process.wait() for name, process in running.items()}


def exited(
    seats: Sequence[registry.Seat],
    bare: Callable[[str], bool | None],
    *,
    wait_s: float = SETTLE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """The seats whose agent exited back to a bare shell within ``wait_s`` of their launch."""
    watching = {seat.seat: seat.pane_id for seat in seats}
    gone: list[str] = []
    for _ in range(round(wait_s / POLL_S)):
        sleep(POLL_S)
        gone += [name for name, pane in watching.items() if bare(pane)]
        watching = {name: pane for name, pane in watching.items() if name not in gone}
        if not watching:
            break
    return sorted(gone)


def relaunch(
    folder: Path,
    seat_name: str,
    base: Mapping[str, str],
    *,
    bare: Callable[[str], bool | None],
    start: Start = init_agent,
    wait_s: float = WAIT_BARE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Start the successor of a pending seat once its pane is back at a bare shell; the outcome.

    The seat stays launching while the successor is watched for SETTLE_S seconds, whatever ``wait_s`` says; one whose
    agent exits back to a bare shell in that window leaves the seat pending.
    """
    taken = registry.take(folder, seat_name, registry.PENDING, registry.LAUNCHING)
    swarm = registry.load(folder)
    if taken is None or swarm is None:
        return "not-pending"
    deadline = time.monotonic() + wait_s
    while not bare(taken.pane_id):
        if time.monotonic() >= deadline:
            registry.set_state(folder, seat_name, registry.PENDING)
            return "pane-busy"
        sleep(POLL_S)
    code, gone = 1, True
    try:
        code = start_seat(folder, swarm, taken, base, start)
        gone = code != 0 or bool(exited([taken], bare, sleep=sleep))
    finally:
        registry.set_state(folder, seat_name, registry.PENDING if gone else registry.READY)
    if code != 0:
        return f"failed-{code}"
    return "exited" if gone else "started"
