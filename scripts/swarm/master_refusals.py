"""Each placement refusal a master seat meets, kept in swarm Redis per seat with its reason and time for a day, so an
empty master seat stays explained after the pass, after the outage hand back and after the controller log rotates."""

import json
import time
from typing import Any

from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.swarm.store import MASTER

KEY = "master-refusals"
KEEP_MS = 24 * 60 * 60_000
ROWS = 100
MINUTE_MS = 60_000
CLOCK = "%H:%M"
NAMED = "master placement last refused {ago} minutes ago, at {at} UTC: {reason}"
NOTICE = "{text} The master placement was last refused {ago} minutes ago, at {at} UTC: {reason}."
LINE = "master placement refused  {seat}  {at} UTC  {ago} minutes ago  {passes} passes since {since} UTC  {reason}"


def record(store: Any, slug: str, seat: str, reason: str, now_ms: int) -> list[str]:
    """Keep the seat's refusal and return it as the step's action: a repeat of the seat's latest reason extends its
    row, a new reason opens one, and the seat keeps its newest rows seen within the day."""
    rows = read(store, slug, seat)
    if rows and rows[-1]["reason"] == reason:
        rows[-1] = {**rows[-1], "at": now_ms, "passes": rows[-1]["passes"] + 1}
    else:
        rows.append({"reason": reason, "since": now_ms, "at": now_ms, "passes": 1})
    kept = [row for row in rows if now_ms - row["at"] < KEEP_MS][-ROWS:]
    key = store.key(slug, KEY)
    store.redis.hset(key, seat, json.dumps(kept))
    store.redis.pexpire(key, KEEP_MS)
    return [reason]


def read(store: Any, slug: str, seat: str) -> list[dict[str, Any]]:
    """Every kept refusal row of the seat, oldest first."""
    raw = store.redis.hget(store.key(slug, KEY), seat)
    return json.loads(raw) if raw else []


def seats(store: Any, slug: str) -> dict[str, list[dict[str, Any]]]:
    """Every seat's kept refusal rows, by seat, paged ROWS seats at a time for at most ROWS pages."""
    key, cursor = store.key(slug, KEY), 0
    fields: dict[str, str] = {}
    for _ in range(ROWS):
        cursor, page = store.redis.hscan(key, cursor, count=ROWS)
        fields.update(page)
        if not cursor:
            break
    return {seat: json.loads(raw) for seat, raw in fields.items()}


def latest(store: Any, slug: str, seat: str) -> dict[str, Any]:
    """The seat's newest kept refusal row, empty while none is kept."""
    rows = read(store, slug, seat)
    return rows[-1] if rows else {}


def named(store: Any, slug: str, now_ms: int) -> str:
    """The front door seat's newest refusal reason within the day with its age and time, empty while none is kept."""
    row = _current(store, slug, now_ms)
    return NAMED.format(ago=_ago(row, now_ms), at=_clock(row["at"]), reason=row["reason"]) if row else ""


def notice(store: Any, slug: str, text: str, now_ms: int) -> str:
    """The master down notice text naming the front door seat's newest refusal within the day with its age and time."""
    row = _current(store, slug, now_ms)
    return NOTICE.format(text=text, ago=_ago(row, now_ms), at=_clock(row["at"]), reason=row["reason"]) if row else text


def status_lines(store: Any, slug: str, now_ms: int) -> list[str]:
    """One swarm status line per master seat naming its newest kept refusal."""
    return [
        LINE.format(
            seat=seat_address(slug, seat),
            at=_clock(rows[-1]["at"]),
            ago=_ago(rows[-1], now_ms),
            passes=rows[-1]["passes"],
            since=_clock(rows[-1]["since"]),
            reason=rows[-1]["reason"],
        )
        for seat, rows in sorted(seats(store, slug).items())
        if rows and now_ms - rows[-1]["at"] < KEEP_MS
    ]


def _current(store: Any, slug: str, now_ms: int) -> dict[str, Any]:
    """The front door seat's newest refusal row while it is within the day, else empty."""
    row = latest(store, slug, MASTER)
    return row if row and now_ms - row["at"] < KEEP_MS else {}


def _ago(row: dict[str, Any], now_ms: int) -> int:
    """Whole minutes since the row's latest refusal."""
    return (now_ms - row["at"]) // MINUTE_MS


def _clock(at_ms: int) -> str:
    """Hours and minutes in UTC of a millisecond time."""
    return time.strftime(CLOCK, time.gmtime(at_ms / 1000))
