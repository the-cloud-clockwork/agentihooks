import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.scheduler.core import Entry, Scheduler
from scripts.scheduler.slots import BroadcastSlot, InboxSlot, Slot, SwarmSlot

TARGETS = ("fleet", "swarm", "seat", "session")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
OPEN = ("ready", "active", "pending")


class CommandError(ValueError):
    pass


def connect() -> Scheduler:
    from scripts.scheduler.service import ensure_running

    scheduler = Scheduler()
    ensure_running()
    return scheduler


def target_slot(target: str, name: str = "") -> Slot:
    if target not in TARGETS:
        raise CommandError(f"target must be one of {', '.join(TARGETS)}, not {target}")
    if target == "fleet":
        if name:
            raise CommandError("the fleet target takes no name")
        return BroadcastSlot()
    if not name:
        raise CommandError(f"the {target} target needs a name")
    if target == "swarm":
        return SwarmSlot(name)
    from scripts.inbox.seats import is_seat

    if is_seat(name) != (target == "seat"):
        raise CommandError(f"{name} is not a {target} address")
    return InboxSlot(name)


def seconds(text: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if match is None:
        raise CommandError(f"{text} is not a duration such as 90, 90s, 5m or 2h")
    return float(match[1]) * UNITS[match[2] or "s"]


def moment(text: str) -> float:
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise CommandError(f"{text} is not an ISO time with an offset such as 2026-10-18T09:30:00+00:00")
    return parsed.timestamp()


def describe(record: dict) -> dict:
    view = {key: record[key] for key in ("id", "kind", "state", "owner", "payload", "due")}
    view["targets"] = [{k: v for k, v in slot.items() if k not in ("id", "signal")} for slot in record["slots"]]
    if record["kind"] == "cron":
        view["expression"], view["timezone"] = record["expression"], record["timezone"]
    if record["kind"] == "stopwatch":
        view["limit"] = record["delay"]
    return view


def _reachable(scheduler: Scheduler, slot: Slot) -> None:
    if not isinstance(slot, SwarmSlot):
        return
    from scripts.scheduler.slots import swarm_store
    from scripts.swarm.store import SwarmError

    try:
        swarm_store(scheduler.state).config(slot.slug)
    except SwarmError as exc:
        raise CommandError(str(exc)) from None


def _arm(entry: Entry, slot: Slot, now: float) -> dict:
    signal = entry.limit_reached if entry.record["kind"] == "stopwatch" else entry.timeout
    try:
        signal.connect(slot)
        entry.start(now=now)
    except Exception:
        entry.cancel()
        raise
    return describe(entry.record)


@contextmanager
def _refusal() -> Iterator[None]:
    try:
        yield
    except ValueError as exc:
        raise CommandError(str(exc)) from None


def add_timer(
    scheduler: Scheduler,
    owner: str,
    slot: Slot,
    message: str,
    *,
    delay: float | None = None,
    at: float | None = None,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    _reachable(scheduler, slot)
    with _refusal():
        entry = scheduler.timer(owner=owner, payload=message, delay=delay, at=at)
    return _arm(entry, slot, now)


def start_stopwatch(
    scheduler: Scheduler, owner: str, slot: Slot, message: str, *, limit: float, now: float | None = None
) -> dict:
    now = time.time() if now is None else now
    _reachable(scheduler, slot)
    with _refusal():
        entry = scheduler.stopwatch(owner=owner, payload=message, limit=limit)
    return _arm(entry, slot, now)


def add_cron(
    scheduler: Scheduler,
    owner: str,
    slot: Slot,
    message: str,
    *,
    expression: str,
    timezone: str = "UTC",
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    _reachable(scheduler, slot)
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise CommandError(f"{timezone} is not a time zone") from None
    with _refusal():
        entry = scheduler.cron(owner=owner, payload=message, expression=expression, timezone=timezone)
    return _arm(entry, slot, now)


def entries(scheduler: Scheduler, kind: str) -> list[dict]:
    return sorted(
        (describe(record) for record in scheduler.records() if record["kind"] == kind and record["state"] in OPEN),
        key=lambda view: view["due"],
    )


def _get(scheduler: Scheduler, kind: str, entry_id: str) -> Entry:
    try:
        entry = scheduler.get(entry_id)
    except KeyError:
        raise CommandError(f"no {kind} {entry_id}") from None
    if entry.record["kind"] != kind:
        raise CommandError(f"no {kind} {entry_id}")
    return entry


def _open(scheduler: Scheduler, kind: str, entry_id: str) -> Entry:
    entry = _get(scheduler, kind, entry_id)
    state = entry.record["state"]
    if state not in OPEN:
        raise CommandError(f"{kind} {entry_id} is already {state}")
    return entry


def cancel(scheduler: Scheduler, kind: str, entry_id: str) -> dict:
    entry = _open(scheduler, kind, entry_id)
    entry.cancel()
    return describe(entry.record)


def stop_stopwatch(scheduler: Scheduler, entry_id: str, *, now: float | None = None) -> dict:
    entry = _open(scheduler, "stopwatch", entry_id)
    entry.stop(now=now)
    return show_stopwatch(scheduler, entry_id)


def show_stopwatch(scheduler: Scheduler, entry_id: str, *, now: float | None = None) -> dict:
    entry = _get(scheduler, "stopwatch", entry_id)
    return {**describe(entry.record), "elapsed": entry.elapsed(now=now)}
