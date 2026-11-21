"""The standing master seat crons: one code owned list the agentihooks scheduler keeps aimed at the front door seat,
ensured when a swarm starts or its front door master joins and cancelled when the swarm stops or closes."""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.inbox.seats import SeatRegistry
from scripts.scheduler import commands
from scripts.scheduler.core import Scheduler
from scripts.scheduler.lease import Busy, held
from scripts.scheduler.slots import InboxSlot
from scripts.swarm import naming, safety_nudge
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import front_door

OWNER = "agentihooks master seat crons"
LOCK = "seat-crons-lock"
WAIT_S = 30
ENDED = ("stopping", "stopped")
QUIET = "Write nothing to the operator."
EMAIL_ROOT = ".swarm-progress-notification-email"


@dataclass(frozen=True)
class Place:
    """Where one swarm's seat crons point: its slug and the master seat files its optional crons follow."""

    slug: str
    sheet: Path
    email: Path


@dataclass(frozen=True)
class SeatCron:
    """One standing master seat cron: its schedule, the prompt it delivers and whether a swarm wants it."""

    expression: str
    prompt: str
    wanted: Callable[[Place], bool]


def _always(place: Place) -> bool:
    return True


def _sheet(place: Place) -> bool:
    return place.sheet.is_file()


def _email(place: Place) -> bool:
    try:
        return json.loads(place.email.read_text()).get("enabled") is True
    except (OSError, ValueError):
        return False


CRONS = (
    SeatCron(
        "3,23,53 * * * *",
        "Master loop (seat cron): read agentihooks swarm {slug} status, clear every focus whose tasks are claimed, in a "
        "pull request or done, list the urgent tasks and each open pull request of a focused task with what blocks it "
        "and what can run in parallel, queue priority for green unblockers, reopen pull request tasks whose owner is "
        "gone and decide every follow up. Act on each. " + QUIET,
        _always,
    ),
    SeatCron(
        "7,37 * * * *",
        "Drift check (seat cron): read agentihooks swarm {slug} status and every target a merged fix touches, the hub, "
        "controller, hive join and hive images, against the newest release and dev; reopen, focus or roll what is "
        "stuck. " + QUIET,
        _always,
    ),
    SeatCron(
        "11,41 * * * *",
        "Progress sheet (seat cron): cd {folder} and run python3 {sheet} run; it rewrites the operator progress sheet. "
        "Act on anything it shows stalled. " + QUIET,
        _sheet,
    ),
    SeatCron(
        "13,43 * * * *",
        "Progress email due check (seat cron): run the swarm-progress-notification-email skill's due_check.sh with "
        "{email}. On no stop. On yes follow the skill steps from lock acquire through mark sent. " + QUIET,
        _email,
    ),
    SeatCron(
        "17 * * * *",
        safety_nudge.TEXT + ", read agentihooks swarm {slug} status for the last tick pass, the controller, spawns "
        "held, host room and the front door seat, and act on whatever it finds stalled, crashed or stale. " + QUIET,
        _always,
    ),
)


def place(config: SwarmConfig, home: Path) -> Place:
    """The master seat files a swarm's optional seat crons follow, under the user home and the swarm repo name."""
    repo = Path(config.repo).name
    sheet = home / "scratchpad" / repo / f"{config.slug}-master" / "progress-sheet" / "progress_sheet.py"
    return Place(config.slug, sheet, home / EMAIL_ROOT / repo / "state.json")


def scheduler() -> Scheduler:
    """This host's scheduler, started when it is not running."""
    return commands.connect()


def swarms() -> RedisStore:
    """The swarm store a ledger join reads the swarm and its master seats from."""
    from scripts.swarm import store

    return store.connect()  # pyright: ignore[reportUnknownMemberType]


def aimed(on: Scheduler, seat: str) -> list[dict[str, Any]]:
    """The open crons aimed at the seat's inbox, the soonest due first."""
    target = {"kind": "inbox", "address": seat}
    return [view for view in commands.entries(on, "cron") if target in view["targets"]]


def _drop(on: Scheduler, views: list[dict[str, Any]]) -> None:
    for view in views:
        commands.cancel(on, "cron", view["id"])


def ensure(on: Scheduler, config: SwarmConfig, home: Path) -> None:
    """Keep each seat cron the swarm wants on the scheduler: the first live cron aimed at the front door seat on its
    schedule is adopted, the rest are cancelled, a missing one is added and an unwanted one is cancelled. A stopping or
    stopped swarm wants none."""
    where = place(config, home)
    seat = front_door.seat(config.slug)
    with held(on.state, on.name(LOCK), WAIT_S):
        live = aimed(on, seat)
        for cron in CRONS:
            found = [view for view in live if view["expression"] == cron.expression]
            if config.state in ENDED or not cron.wanted(where):
                _drop(on, found)
                continue
            _drop(on, found[1:])
            text = cron.prompt.format(
                slug=where.slug, folder=where.sheet.parent, sheet=where.sheet, email=where.email.parent
            )
            if not found:
                commands.add_cron(on, OWNER, InboxSlot(seat), text, expression=cron.expression)


def cancel(on: Scheduler, slug: str) -> None:
    """Cancel every live cron aimed at the swarm's front door seat on a listed schedule, hand made ones included."""
    listed = {cron.expression for cron in CRONS}
    with held(on.state, on.name(LOCK), WAIT_S):
        gone = [view for view in aimed(on, front_door.seat(slug)) if view["expression"] in listed]
        _drop(on, gone)


def _guarded(slug: str, done: str, step: Callable[[], object]) -> None:
    from scripts.events.state import unavailable

    try:
        step()
    except (*unavailable(), SwarmError, Busy, commands.CommandError) as exc:
        print(f"warning: the master seat crons of {slug} were not {done}: {exc}", file=sys.stderr)


def on_start(store: RedisStore, slug: str) -> None:
    """Ensure the seat crons as a swarm starts; a scheduler this host cannot reach leaves a warning, never a failed
    start."""
    _guarded(slug, "ensured", lambda: ensure(scheduler(), store.config(slug), Path.home()))


def on_stop(slug: str) -> None:
    """Cancel the seat crons as a swarm stops or closes."""
    _guarded(slug, "cancelled", lambda: cancel(scheduler(), slug))


def _join(slug: str, name: str) -> None:
    store = swarms()
    seats = SeatRegistry(store.redis)  # pyright: ignore[reportUnknownMemberType]
    if front_door.holds(slug, seats.seat_of(name)):
        ensure(scheduler(), store.config(slug), Path.home())


def on_join(slug: str, name: str) -> None:
    """Ensure the seat crons when the front door master joins. A backend master's join adds none, because each host
    keeps its own scheduler and a second set would deliver every reminder twice."""
    if naming.lane_of(name) == MASTER:
        _guarded(slug, "ensured", lambda: _join(slug, name))
