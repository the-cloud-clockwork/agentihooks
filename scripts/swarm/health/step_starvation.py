"""Tick step starvation: each step's last completed run, read from the pass records, and an alarm once a step that ran
has not completed within three of its windows.

A pass record row counts as a completed run when its outcome is ok, or late without being abandoned. A row resting on
its interval or a closed gate is not a run. Every other row (abandoned, cut, held, failed or dropped) is a miss: the
first miss after a completed run starts the starvation clock and each miss counts one pass. A step whose clock passes
three windows, its interval from budgets.FLOORS at the tick cadence, sends the master seat one urgent inbox item per
starvation and is listed as a health finding in swarm status.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import timing
from scripts.swarm.health.findings import Finding

KEY = "step-starvation"
RECORDS = "tick:records"
KIND = "step starvation"
SENDER = "swarm"
MASTER = "master"
WINDOWS = 3
READ_LIMIT = 2000
COMPLETED = frozenset({"ok", "late"})
SPOKEN = "%d %B %H:%M UTC"


def read(store: Any, slug: str) -> dict[str, Any]:
    """Return the saved state: the records cursor, each step's completion or miss entry and each step's window."""
    raw = store.redis.get(store.key(slug, KEY))
    return dict[str, Any](json.loads(raw)) if raw else {}


def _save(store: Any, slug: str, state: Mapping[str, Any]) -> None:
    timing.keep()
    store.redis.set(store.key(slug, KEY), json.dumps(state))


def completed(row: Mapping[str, Any]) -> bool:
    """Return whether a pass record ``row`` is a run that ended: ok, or late without being abandoned."""
    return row.get("outcome") in COMPLETED and not row.get("abandoned")


def note(steps: dict[str, dict[str, int]], row: Mapping[str, Any], at: int) -> None:
    """Fold one pass record ``row`` journaled at ``at`` into ``steps``: a completion resets its step, a run that did not
    complete starts or extends its misses, a rested row leaves it."""
    name = str(row["step"])
    if completed(row):
        steps[name] = {"done": at}
        return
    if row.get("rested"):
        return
    entry = steps.setdefault(name, {})
    entry.setdefault("missed", at)
    entry["misses"] = entry.get("misses", 0) + 1


def _rows(store: Any, slug: str, cursor: str) -> list[tuple[str, dict[str, Any]]]:
    found = store.redis.xrange(store.key(slug, RECORDS), min=f"({cursor}", max="+", count=READ_LIMIT)
    return [(str(entry), dict[str, Any](json.loads(fields["record"]))) for entry, fields in found]


def observe(store: Any, slug: str, at: int, windows: Mapping[str, float]) -> dict[str, Any]:
    """Read the pass records journaled since the cursor into each step's last completion or misses, keep ``windows``
    and save the state; the first read starts at ``at``."""
    state = read(store, slug)
    cursor = str(state.get("cursor") or f"{at}-0")
    steps = dict[str, dict[str, int]](state.get("steps", {}))
    for entry, row in _rows(store, slug, cursor):
        note(steps, row, int(entry.partition("-")[0]))
        cursor = entry
    state = {"cursor": cursor, "steps": steps, "windows": dict(windows)}
    _save(store, slug, state)
    return state


def starved(state: Mapping[str, Any], at: int) -> list[tuple[str, dict[str, int], float]]:
    """Return each step whose first miss since its last completion is three windows or more before ``at``."""
    windows = dict[str, float](state.get("windows", {}))
    return [
        (name, entry, windows[name])
        for name, entry in sorted(dict[str, dict[str, int]](state.get("steps", {})).items())
        if "missed" in entry and name in windows and at - entry["missed"] >= WINDOWS * windows[name] * 1000
    ]


def _spoken(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime(SPOKEN)


def _evidence(entry: Mapping[str, int], window: float) -> tuple[str, ...]:
    last = _spoken(entry["done"]) if "done" in entry else "never since the watch began"
    return (
        f"Last completed run: {last}",
        f"{entry['misses']} passes since ran it without a completed run, the first at {_spoken(entry['missed'])}",
        f"Window {window:g}s from budgets.FLOORS; three windows is {WINDOWS * window:g}s",
    )


def _summary(name: str, entry: Mapping[str, int], at: int) -> str:
    since = entry.get("done", entry["missed"])
    return f"Tick step {name} has not completed for {(at - since) // 60_000} minutes"


def _finding(name: str, entry: Mapping[str, int], window: float, at: int) -> Finding:
    summary, evidence = _summary(name, entry, at), _evidence(entry, window)
    return Finding(KIND, name.replace(" ", "-"), summary, evidence, "three windows", entry["misses"])


def findings(store: Any, slug: str, at: int) -> list[Finding]:
    """Return one health finding per starved tick step: the step, its last completion and the passes since."""
    return [_finding(name, entry, window, at) for name, entry, window in starved(read(store, slug), at)]


def alarm(store: Any, slug: str, state: Mapping[str, Any], at: int) -> list[str]:
    """Send the master seat one urgent inbox item per starved step and starvation, returning a line per item sent."""
    master = str(seat_address(slug, MASTER))
    lines: list[str] = []
    for name, entry, window in starved(state, at):
        summary = _summary(name, entry, at)
        text = f"URGENT: {summary} on swarm {slug}. Evidence: {'; '.join(_evidence(entry, window))}."
        item_id = hashlib.sha256(f"{KEY}:{slug}:{name}:{entry['missed']}".encode()).hexdigest()[:12]
        try:
            InboxStore(store.redis).send(SENDER, master, text, ref=f"{KEY}:{slug}:{name}", item_id=item_id)
        except DuplicateItem:
            continue
        lines.append(f"step starvation: alarmed {master}: {summary}")
    return lines


def step(store: Any, slug: str, at: int, windows: Mapping[str, float]) -> list[str]:
    """Record each step's last completed run from the pass records and alarm the master for each starved step."""
    return alarm(store, slug, observe(store, slug, at, windows), at)
