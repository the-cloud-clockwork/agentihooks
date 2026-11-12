"""The quick tick: the round, run by the hourly safety nudge, that nudges idle engineers, tells seats of room lines,
relaunches pending seats and starts again an engineer whose agent exited; it never types into a bare shell."""

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts import herdr_host
from scripts.quick import paths, registry, room, seeds
from scripts.quick.layout import MASTER

NUDGE = "Quick swarm nudge: continue the next open stage of your plan, or tell the master what blocks you."
ROOM = "Quick swarm room: {count} message(s) for you; read them with `agentihooks quick room`."
NUDGE_EVERY_S = 600
READY_STATES = ("idle", "done")
Relaunch = Callable[[Path, str], str]
Row = dict[str, Any]


class Herdr(Protocol):
    """The herdr pane reads and prompt the quick tick uses."""

    def status(self, pane_id: str) -> str:
        """The agent status herdr reports for the pane."""
        ...

    def typed(self, pane_id: str) -> str:
        """Text waiting on the pane's input line."""
        ...

    def bare(self, pane_id: str) -> bool | None:
        """Whether only a shell runs in the pane; None when herdr cannot say."""
        ...

    def prompt(self, pane_id: str, text: str) -> bool:
        """Submit ``text`` as a prompt to the pane's agent."""
        ...


@dataclass(frozen=True)
class LiveHerdr:
    """The live herdr server."""

    environ: dict[str, str]

    def status(self, pane_id: str) -> str:
        """The agent status herdr reports for the pane."""
        return herdr_host.agent_status(pane_id, self.environ)

    def typed(self, pane_id: str) -> str:
        """Text waiting on the pane's input line."""
        return herdr_host.typed(pane_id, self.environ)

    def bare(self, pane_id: str) -> bool | None:
        """Whether only a shell runs in the pane; None when herdr cannot say, as for a pane it no longer has."""
        from scripts.herdr_gc import bare_shell

        try:
            info = herdr_host.process_info(pane_id, self.environ)
        except (herdr_host.HerdrError, subprocess.TimeoutExpired):
            return None
        return bare_shell(info) if info.get("foreground_processes") else None

    def prompt(self, pane_id: str, text: str) -> bool:
        """Submit ``text`` as a prompt to the pane's agent."""
        return herdr_host.prompt(pane_id, text, self.environ)


def _stamp(folder: Path, seat: str, name: str) -> Path:
    return paths.seat_dir(folder, seat) / name


def _number(path: Path) -> float:
    try:
        return float(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _open_stages(folder: Path, seat: str) -> bool:
    done, total, _ = seeds.plan_state(paths.read_text(paths.plan(folder, seat)))
    return done < total


def _room_notice(folder: Path, seat: registry.Seat, herdr: Herdr) -> bool:
    count = room.waiting(folder, seat.seat)
    told = _stamp(folder, seat.seat, "room.told")
    if count < _number(told):
        told.write_text(str(count))
    if count <= _number(told) or not herdr.prompt(seat.pane_id, ROOM.format(count=count)):
        return False
    told.write_text(str(count))
    return True


def _nudge(folder: Path, seat: registry.Seat, herdr: Herdr, now: float) -> bool:
    nudged = _stamp(folder, seat.seat, "nudged.at")
    due = seat.seat != MASTER and _open_stages(folder, seat.seat) and now - _number(nudged) >= NUDGE_EVERY_S
    if not due or not herdr.prompt(seat.pane_id, NUDGE):
        return False
    nudged.write_text(str(now))
    return True


def _busy(seat: registry.Seat, herdr: Herdr) -> str:
    shell = herdr.bare(seat.pane_id)
    if shell is not False:
        return "skipped-bare" if shell else "skipped-unknown"
    status = herdr.status(seat.pane_id)
    if status not in READY_STATES:
        return f"skipped-{status}"
    return "skipped-typed" if herdr.typed(seat.pane_id) else ""


def tell(folder: Path, seat: registry.Seat, herdr: Herdr) -> str:
    """Tell an idle seat, with nothing typed in its pane, that room lines wait for it; the outcome."""
    why = _busy(seat, herdr)
    if why:
        return why
    return "room" if _room_notice(folder, seat, herdr) else "idle"


def _ready_seat(folder: Path, seat: registry.Seat, herdr: Herdr, now: float) -> str:
    why = _busy(seat, herdr)
    if why:
        return why
    if _room_notice(folder, seat, herdr):
        return "room"
    return "nudged" if _nudge(folder, seat, herdr, now) else "idle"


def _outcome(folder: Path, seat: registry.Seat, herdr: Herdr, relaunch: Relaunch, now: float) -> str:
    if seat.state == registry.PENDING:
        return f"relaunched-{relaunch(folder, seat.seat)}" if herdr.bare(seat.pane_id) else "pending"
    if seat.state != registry.READY:
        return seat.state
    exited = seat.seat != MASTER and herdr.bare(seat.pane_id)
    if exited and registry.take(folder, seat.seat, registry.READY, registry.PENDING):
        return f"revived-{relaunch(folder, seat.seat)}"
    return _ready_seat(folder, seat, herdr, now)


def round_for(folder: Path, herdr: Herdr, relaunch: Relaunch, now: float) -> list[Row]:
    """One tick over one quick swarm: an outcome per seat."""
    swarm = registry.load(folder)
    if swarm is None:
        return []
    return [{"seat": seat.seat, "outcome": _outcome(folder, seat, herdr, relaunch, now)} for seat in swarm.seats]


def run_round(environ: Mapping[str, str], herdr: Herdr | None = None) -> list[Row]:
    """One tick over every live quick swarm on this host, run by the hourly safety nudge round."""
    from scripts.quick import launch

    live = herdr or LiveHerdr(dict(environ))

    def relaunch(folder: Path, seat: str) -> str:
        return launch.relaunch(folder, seat, environ, bare=live.bare, wait_s=0.0)

    now = time.time()
    rows: list[Row] = []
    for folder in paths.folders(environ):
        rows += [{"folder": str(folder), **row} for row in round_for(folder, live, relaunch, now)]
    return rows
