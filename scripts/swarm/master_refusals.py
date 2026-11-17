"""Each master placement refusal the tick master step meets, kept in swarm Redis with its reason and time for the
sixty minute finding window, so an empty front door master seat stays explained after the pass, after the outage hand
back and after the controller log rotates."""

import json
import time
from typing import Any

KEY = "master-refusals"
KEEP_MS = 60 * 60_000
MINUTE_MS = 60_000
CLOCK = "%H:%M"
NAMED = "master placement refused at {at} UTC: {reason}"
NOTICE = "{text} The latest master placement refusal, at {at} UTC: {reason}."
LINE = "master placement refused  {at} UTC  {ago} minutes ago  {passes} passes since {since} UTC  {reason}"


def refused(store: Any, slug: str, reason: str, now_ms: int) -> list[str]:
    """Keep the refusal and return it as the master step's action: a repeat of the latest reason extends its row, a
    new reason opens one, and a row last seen a whole window ago is dropped."""
    rows = read(store, slug)
    if rows and rows[-1]["reason"] == reason:
        rows[-1] = {**rows[-1], "at": now_ms, "passes": rows[-1]["passes"] + 1}
    else:
        rows.append({"reason": reason, "since": now_ms, "at": now_ms, "passes": 1})
    kept = [row for row in rows if now_ms - row["at"] < KEEP_MS]
    store.redis.set(store.key(slug, KEY), json.dumps(kept), px=KEEP_MS)
    return [reason]


def read(store: Any, slug: str) -> list[dict[str, Any]]:
    """Every kept refusal row, oldest first."""
    raw = store.redis.get(store.key(slug, KEY))
    return list[dict[str, Any]](json.loads(raw)) if raw else []


def latest(store: Any, slug: str) -> dict[str, Any]:
    """The newest kept refusal row, empty while none is kept."""
    rows = read(store, slug)
    return rows[-1] if rows else {}


def named(store: Any, slug: str) -> str:
    """The newest refusal reason with its time, empty while none is kept."""
    row = latest(store, slug)
    return NAMED.format(at=_clock(row["at"]), reason=row["reason"]) if row else ""


def notice(store: Any, slug: str, text: str) -> str:
    """The master down notice text naming the newest refusal reason and its time."""
    row = latest(store, slug)
    return NOTICE.format(text=text, at=_clock(row["at"]), reason=row["reason"]) if row else text


def status_lines(store: Any, slug: str, now_ms: int) -> list[str]:
    """The swarm status line for the newest refusal, none while none is kept."""
    row = latest(store, slug)
    if not row:
        return []
    ago = (now_ms - row["at"]) // MINUTE_MS
    return [
        LINE.format(
            at=_clock(row["at"]), ago=ago, passes=row["passes"], since=_clock(row["since"]), reason=row["reason"]
        )
    ]


def _clock(at_ms: int) -> str:
    """Hours and minutes in UTC of a millisecond time."""
    return time.strftime(CLOCK, time.gmtime(at_ms / 1000))
