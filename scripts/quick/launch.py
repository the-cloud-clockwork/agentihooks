"""Launch a quick seat's agent into its own herdr pane, first at quick up and again after each handoff."""

import contextlib
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from scripts.quick import paths, recipe, registry, seeds, tick
from scripts.quick.member import DIR_ENV, QUICK_ENV, SEAT_ENV, Member

WAIT_BARE_S = 60.0
SETTLE_S = 20.0
BIND_S = 90.0
POLL_S = 1.0
END_TIMEOUT_S = 120.0
SWARM_KEYS = ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_AGENT_NAME")
Start = Callable[[list[str], dict[str, str]], int]


def argv(swarm: registry.Swarm, seat: registry.Seat, prompt_file: Path) -> list[str]:
    selection = seat.auth if seat.auth == "default" else f"named:{seat.account}"
    policy = ["--routed-only"] if seat.auth == "routed" else ["--auth", selection]
    return [
        *("--host", "herdr", "--pane", seat.pane_id, "--name", seat.name, "--agent", seat.harness),
        *("--dir", swarm.directory, "--prompt-file", str(prompt_file)),
        *policy,
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


def start_command(folder: Path, swarm: registry.Swarm, seat: registry.Seat, base: Mapping[str, str]) -> int:
    env = environ(base, folder, swarm, seat)
    exports = {name: env[name] for name in (QUICK_ENV, SEAT_ENV, DIR_ENV)}
    if "PATH" in base:
        exports["PATH"] = base["PATH"]
    seat_dir = paths.seat_dir(folder, seat.seat)
    recipe.forget(seat_dir)
    launcher = seat_dir / recipe.LAUNCHER
    launcher.write_text(recipe.script(seat.command, exports, seat_dir / recipe.PID, swarm.directory))
    launcher.chmod(0o700)
    return 0 if tick.LiveHerdr(env).run(seat.pane_id, launcher) else 1


def start_seat(folder: Path, swarm: registry.Swarm, seat: registry.Seat, base: Mapping[str, str], start: Start) -> int:
    """Open the seat's agent, or run its saved command, in its pane with its opening prompt."""
    prompt_file = _write_opening(Member(folder, swarm, seat))
    if recipe.kind(seat) == recipe.PROGRAM:
        return start_command(folder, swarm, seat, base)
    return start(argv(swarm, seat, prompt_file), environ(base, folder, swarm, seat))


def start_all(folder: Path, swarm: registry.Swarm, base: Mapping[str, str]) -> dict[str, int]:
    running: dict[str, subprocess.Popen[bytes]] = {}
    taken: dict[str, registry.Seat] = {}
    codes: dict[str, int] = {}
    try:
        for seat in swarm.seats[1:]:
            if seat.state not in (registry.READY, registry.PENDING):
                continue
            if registry.take(folder, seat.seat, seat.state, registry.LAUNCHING) is None:
                continue
            taken[seat.seat] = seat
            if recipe.kind(seat) == recipe.PROGRAM:
                _write_opening(Member(folder, swarm, seat))
                codes[seat.seat] = start_command(folder, swarm, seat, base)
                continue
            prompt_file = _write_opening(Member(folder, swarm, seat))
            command = [sys.executable, "-m", "scripts.init_agent", *argv(swarm, seat, prompt_file)]
            running[seat.seat] = subprocess.Popen(command, env=environ(base, folder, swarm, seat))
        for name, process in running.items():
            codes[name] = process.wait()
        return codes
    finally:
        for name, seat in taken.items():
            recorded = codes.get(name) == 0 and (
                program_ready(folder, seat, sleep=time.sleep)
                if recipe.kind(seat) == recipe.PROGRAM
                else _recorded(folder, seat)
            )
            state = registry.READY if codes.get(name) == 0 and recorded else registry.PENDING
            registry.set_state(folder, name, state)


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


def _recorded(folder: Path, before: registry.Seat) -> bool:
    swarm = registry.load(folder)
    seat = swarm.seat(before.seat) if swarm else None
    return seat is not None and seat.session_id not in ("", before.session_id)


def bound(folder: Path, before: registry.Seat, *, sleep: Callable[[float], None], wait_s: float = BIND_S) -> bool:
    """Whether the seat's new agent recorded a session other than its predecessor's within ``wait_s``."""
    for _ in range(round(wait_s / POLL_S)):
        if _recorded(folder, before):
            return True
        sleep(POLL_S)
    return _recorded(folder, before)


def program_ready(folder: Path, before: registry.Seat, *, sleep: Callable[[float], None]) -> bool:
    assert recipe.kind(before) == recipe.PROGRAM
    for _ in range(round(SETTLE_S / POLL_S)):
        if recipe.alive(paths.seat_dir(folder, before.seat)):
            return True
        sleep(POLL_S)
    return recipe.alive(paths.seat_dir(folder, before.seat))


def ready(folder: Path, before: registry.Seat, *, sleep: Callable[[float], None]) -> bool:
    if recipe.kind(before) == recipe.PROGRAM:
        return program_ready(folder, before, sleep=sleep)
    return bound(folder, before, sleep=sleep)


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
    agent exits back to a bare shell in that window leaves the seat pending, and so does one whose agent never records
    its own session in the seat within BIND_S seconds more.
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
    pending = True
    try:
        code = start_seat(folder, swarm, taken, base, start)
        gone = code != 0 or bool(exited([taken], bare, sleep=sleep))
        pending = gone or not ready(folder, taken, sleep=sleep)
    finally:
        registry.set_state(folder, seat_name, registry.PENDING if pending else registry.READY)
    if code != 0:
        return f"failed-{code}"
    if gone:
        return "exited"
    return "unbound" if pending else "started"


def end_agent(name: str) -> None:
    """End the agent session called ``name`` and keep its herdr pane; whether it ended shows as the pane going bare."""
    command = [sys.executable, "-m", "scripts.terminate_agent", name, "--type", "any", "--keep-pane"]
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(command, capture_output=True, check=False, timeout=END_TIMEOUT_S)


def force(
    folder: Path,
    seat_name: str,
    base: Mapping[str, str],
    *,
    bare: Callable[[str], bool | None],
    end: Callable[[str], None] = end_agent,
    start: Start = init_agent,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """End a seat's live but unusable agent with its pane kept, then start its successor; the outcome.

    The seat reads launching while its agent is ended, so no tick starts a successor the end could reach; the
    successor then waits up to WAIT_BARE_S for the pane to return to a bare shell. A seat being launched already
    has a successor coming and is not ended.
    """
    swarm = registry.load(folder)
    seat = swarm.seat(seat_name) if swarm else None
    if seat is None:
        return "no-seat"
    if seat.state == registry.LAUNCHING or registry.take(folder, seat_name, seat.state, registry.LAUNCHING) is None:
        return "launching"
    try:
        if recipe.kind(seat) == recipe.PROGRAM:
            if not recipe.stop(paths.seat_dir(folder, seat.seat)):
                return "unbound"
        else:
            end(seat.name)
    finally:
        registry.set_state(folder, seat_name, registry.PENDING)
    return relaunch(folder, seat_name, base, bare=bare, start=start, sleep=sleep)


def adopt(folder: Path, seat_name: str, pane_id: str, session_id: str, *, bare: Callable[[str], bool | None]) -> str:
    """Make the live session in ``pane_id`` the seat's ready occupant; the outcome.

    A pane another seat holds, a seat whose recorded pane still runs an agent and a seat being launched are never
    adopted.
    """
    swarm = registry.load(folder)
    seat = swarm.seat(seat_name) if swarm else None
    if swarm is None or seat is None:
        return "no-seat"
    holder = swarm.at_pane(pane_id)
    if holder is not None and holder.seat != seat_name:
        return f"pane-of-{holder.seat}"
    if seat.pane_id != pane_id and bare(seat.pane_id) is False:
        return "occupied"
    return "adopted" if registry.adopt(folder, seat_name, pane_id, session_id) else "launching"
