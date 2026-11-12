"""The quick tick: a per-minute round that nudges idle engineers, tells the master of room lines and relaunches."""

import shutil
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
MARKER = "# agentihooks-quick-swarm"
SCHEDULE = "* * * * *"
CRON = "crontab"
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
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

    def bare(self, pane_id: str) -> bool:
        """Whether only a shell runs in the pane."""
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

    def bare(self, pane_id: str) -> bool:
        """Whether only a shell runs in the pane."""
        from scripts.herdr_gc import bare_shell

        return bare_shell(herdr_host.process_info(pane_id, self.environ))

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
    try:
        text = paths.plan(folder, seat).read_text()
    except OSError:
        return False
    done, total, _ = seeds.plan_state(text)
    return done < total


def _ready_seat(folder: Path, seat: registry.Seat, herdr: Herdr, now: float) -> str:
    status = herdr.status(seat.pane_id)
    if status not in READY_STATES:
        return f"skipped-{status}"
    if herdr.typed(seat.pane_id):
        return "skipped-typed"
    count = room.waiting(folder, seat.seat)
    told = _stamp(folder, seat.seat, "room.told")
    if count > _number(told):
        herdr.prompt(seat.pane_id, ROOM.format(count=count))
        told.write_text(str(count))
        return "room"
    nudged = _stamp(folder, seat.seat, "nudged.at")
    if seat.seat != MASTER and _open_stages(folder, seat.seat) and now - _number(nudged) >= NUDGE_EVERY_S:
        herdr.prompt(seat.pane_id, NUDGE)
        nudged.write_text(str(now))
        return "nudged"
    return "idle"


def _outcome(folder: Path, seat: registry.Seat, herdr: Herdr, relaunch: Relaunch, now: float) -> str:
    if seat.state == registry.PENDING:
        return f"relaunched-{relaunch(folder, seat.seat)}" if herdr.bare(seat.pane_id) else "pending"
    if seat.state != registry.READY:
        return seat.state
    return _ready_seat(folder, seat, herdr, now)


def round_for(folder: Path, herdr: Herdr, relaunch: Relaunch, now: float) -> list[Row]:
    """One tick over one quick swarm: an outcome per seat."""
    swarm = registry.load(folder)
    if swarm is None:
        return []
    return [{"seat": seat.seat, "outcome": _outcome(folder, seat, herdr, relaunch, now)} for seat in swarm.seats]


def run_round(environ: Mapping[str, str]) -> list[Row]:
    """One tick over every live quick swarm on this host."""
    from scripts.quick import launch

    live = LiveHerdr(dict(environ))

    def relaunch(folder: Path, seat: str) -> str:
        return launch.relaunch(folder, seat, environ, bare=live.bare, wait_s=0.0)

    now = time.time()
    return [
        {"folder": str(folder), **row}
        for folder in paths.folders(environ)
        for row in round_for(folder, live, relaunch, now)
    ]


def command(environ: Mapping[str, str]) -> str:
    """The cron line running the quick tick every minute with agentihooks and herdr on its PATH."""
    found = [shutil.which(tool, path=environ.get("PATH")) for tool in ("agentihooks", "herdr")]
    exe = found[0] or "agentihooks"
    dirs = dict.fromkeys([*(str(Path(tool).parent) for tool in found if tool), *BASE_PATH])
    socket = environ.get("HERDR_SOCKET_PATH")
    prefix = f"HERDR_SOCKET_PATH={socket} " if socket else ""
    return f"{SCHEDULE} PATH={':'.join(dirs)} {prefix}{exe} quick tick >/dev/null 2>&1 {MARKER}"


def _table() -> list[str]:
    done = subprocess.run([CRON, "-l"], capture_output=True, text=True, check=False)
    return done.stdout.splitlines() if done.returncode == 0 else []


def _write(lines: list[str]) -> None:
    done = subprocess.run([CRON, "-"], input="\n".join(lines) + "\n", capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{CRON} refused the quick tick: {done.stderr.strip()}")


def install(environ: Mapping[str, str]) -> str:
    """Register the per-minute quick tick in the user's cron table, replacing an earlier one."""
    line = command(environ)
    _write([kept for kept in _table() if MARKER not in kept] + [line])
    return line


def remove(environ: Mapping[str, str]) -> None:
    """Drop the quick tick from the user's cron table."""
    _write([kept for kept in _table() if MARKER not in kept])
