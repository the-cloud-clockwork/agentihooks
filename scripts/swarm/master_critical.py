"""Critical no master state, kept per swarm in its master-critical record and driven by the master fallback pass
outside the tick. A running swarm whose front door seat has had no bound master for
AGENTIHOOKS_MASTER_CRITICAL_MINUTES (default 30) is critical: status shows it as a finding, a critical broadcast and
the ledger's seat alert raise it, and the pass forces master up once per retry window while no master start is in
flight. Each forced start's outcome is kept with its time, exit status and reason; a failed one, or one whose master
never binds, raises the broadcast again and puts its reason on the seat alert. A bound front door master, or a swarm
that is not running or sleeps with no work, clears it. The pass and the forced runner read and write the record under
one lock per swarm that no ledger call runs inside, and every pass stamps the swarm's watch beat, so status also shows
a watch that stopped or failed."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hooks.context import broadcast
from scripts.swarm import lifetime, master_start
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import MASTER, SwarmError
from scripts.swarm_v2 import front_door

KEY = "master-critical"
BEAT = "master-critical-beat"
LOCK = "master-critical-lock"
LOCK_S = 20
WAIT_S = 30
MINUTES = "AGENTIHOOKS_MASTER_CRITICAL_MINUTES"
DEFAULT_MINUTES = 30.0
RETRY_MS = 5 * MINUTE_MS
STALE_MS = 5 * MINUTE_MS
KEPT = 10
SOURCE = "swarm"
SEAT = "seat"
STARTED, FAILED = "started", "failed"
NO_OUTCOME = "the forced master up recorded no outcome within five minutes"
NO_BIND = "the master it started did not bind the front door seat within five minutes"
CRITICAL = (
    "CRITICAL: swarm {slug} has had no bound master for {minutes} minutes. The master fallback forces agentihooks "
    "swarm {slug} master up every five minutes until a master binds the front door seat."
)
STILL = "CRITICAL: swarm {slug} still has no master; the forced master up at {at} failed: {reason}"
SEAT_STILL = "CRITICAL: swarm {slug} still has no master; its forced master up failed: {reason}"
REPLACED = "Replaced by a newer critical alert: {text}"
CLEARED = "Swarm {slug} is out of critical mode: {why}."
THRESHOLD = "no bound front door master for AGENTIHOOKS_MASTER_CRITICAL_MINUTES, default thirty minutes"
WATCH_THRESHOLD = "a clean master fallback watch pass within the last five minutes"


@dataclass(frozen=True)
class Change:
    """What one watch pass changed in the record: its output lines, the text the ledger's open seat alert must carry,
    and the outcome that closes the open seat alerts once critical mode ends."""

    lines: list[str]
    seat: str = ""
    close: str = ""


def minutes(environ: Mapping[str, str]) -> float:
    """The minutes a running swarm may go with no bound front door master before it turns critical."""
    return float(environ.get(MINUTES) or DEFAULT_MINUTES)


def read(store: Any, slug: str) -> dict[str, Any]:
    """The swarm's critical record: the outage start, the critical time and the forced starts, empty when none."""
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def bound(store: Any, slug: str, live: set[str]) -> str:
    """The live front door master that holds the seat, empty when the seat has none: one that is working, or one still
    starting whose own hooks beat since its launch, as the tick's master start counts it."""
    return next(
        (
            agent.name
            for agent in store.agents(slug)
            if agent.lane == MASTER and front_door.holds(slug, agent.seat) and _holds(store, slug, agent, live)
        ),
        "",
    )


def _holds(store: Any, slug: str, agent: Any, live: set[str]) -> bool:
    if master_start.seated(agent, live, {}):
        return True
    return agent.name in live and agent.state == "starting" and master_start.hooked(store, slug, agent)


def locked(store: Any, slug: str) -> Any:
    """The swarm's critical record lock, which the pass and the forced runner each hold around a read of the record
    and its write, so neither overwrites the other's update."""
    return store.redis.lock(store.key(slug, LOCK), timeout=LOCK_S, blocking_timeout=WAIT_S)


def active(store: Any, slug: str) -> bool:
    """Whether the swarm is in critical mode now."""
    return "critical_at" in read(store, slug)


def watch(
    store: Any,
    ledger: Any,
    slug: str,
    live: set[str],
    now_ms: int,
    environ: Mapping[str, str],
    force: Callable[[str, int], int],
) -> list[str]:
    """One watchdog pass over a swarm: note when its front door seat lost its bound master, turn critical at the
    threshold and force master up, and clear once a master binds or the swarm stops running or sleeps with no work.
    Only the record's read and write hold the swarm's lock: the ledger is read before the lock is taken and the seat
    alert written after it is released, so a slow ledger never outlasts the lock."""
    why = _cleared_by(store, ledger, slug, live)
    with locked(store, slug):
        change = _pass(store, slug, why, now_ms, environ, force)
    return [*change.lines, *_seat(ledger, slug, change)]


def _cleared_by(store: Any, ledger: Any, slug: str, live: set[str]) -> str:
    """Why the swarm is out of critical mode now: a bound front door master, or a swarm that is not running or sleeps
    with no work; empty while it runs with no bound master."""
    if master := bound(store, slug, live):
        return f"master {master} bound the front door seat"
    if store.config(slug).state != "running" or _sleeping(store, ledger, slug):
        return "the swarm is not running or sleeps with no work"
    return ""


def _pass(
    store: Any, slug: str, why: str, now_ms: int, environ: Mapping[str, str], force: Callable[[str, int], int]
) -> Change:
    """The record's part of a watch pass, run under the swarm's lock."""
    state = read(store, slug)
    if why:
        return _clear(store, slug, state, why)
    state.setdefault("since", now_ms)
    due = now_ms - state["since"] >= minutes(environ) * MINUTE_MS
    change = _critical(store, slug, state, now_ms, force) if due else Change([])
    _save(store, slug, state)
    return change


def _sleeping(store: Any, ledger: Any, slug: str) -> bool:
    """Whether the swarm sleeps with no work. Only a swarm whose master retired with nothing waiting for it has its
    ledger tasks read, to see whether new work came since; a ledger that cannot be read leaves the swarm awake."""
    if not lifetime.sleeping(slug, store, {}):  # pyright: ignore[reportUnknownMemberType]
        return False
    try:
        rows = {task["id"]: task for task in ledger.tasks(slug)}
    except (SwarmError, OSError):
        return False
    return lifetime.sleeping(slug, store, rows, write=False)  # pyright: ignore[reportUnknownMemberType]


def beat(store: Any, slug: str, now_ms: int, error: str) -> None:
    """Stamp the swarm's watch beat with this pass's time and its error, empty for a clean pass."""
    store.redis.set(store.key(slug, BEAT), json.dumps({"at": now_ms, "error": error}))


def unwatch(store: Any, watched: list[str]) -> None:
    """Drop the watch beat of every swarm outside watched, the swarms this master fallback watches, so status shows no
    stopped watch for a swarm the fallback no longer watches."""
    gone = [store.key(slug, BEAT) for slug in store.slugs() if slug not in watched]
    if gone:
        store.redis.delete(*gone)


def ended(store: Any, slug: str, at: int, code: int, output: str, now_ms: int) -> str:
    """Record, under the swarm's lock, how the forced master up started at `at` ended and its exit status; a failure
    raises the critical broadcast again, and an attempt already recorded keeps its outcome."""
    with locked(store, slug):
        state = read(store, slug)
        attempt = next((row for row in state.get("attempts", []) if row["at"] == at), None)
        if attempt is None:
            return f"forced master up of swarm {slug} at {_clock(at)} ended after its critical state cleared"
        if attempt["outcome"]:
            return f"forced master up of swarm {slug} at {_clock(at)} ended after it was recorded {attempt['outcome']}"
        attempt["code"] = code
        if code:
            line = _failed(slug, state, attempt, _reason(output, code), now_ms)
        else:
            attempt.update(outcome=STARTED, ended_at=now_ms)
            line = f"forced master up of swarm {slug} at {_clock(at)} started a master"
        _save(store, slug, state)
        return line


def findings(store: Any, slug: str, now_ms: int) -> list[Finding]:
    """The critical state, and a watch that stopped or failed, as swarm health findings."""
    return _critical_finding(read(store, slug), slug, now_ms) + _watch_finding(store, slug, now_ms)


def _critical_finding(state: dict[str, Any], slug: str, now_ms: int) -> list[Finding]:
    if "critical_at" not in state:
        return []
    waited = (now_ms - state["since"]) // MINUTE_MS
    began = f"No bound master since {_clock(state['since'])}, critical since {_clock(state['critical_at'])}"
    summary = f"No master has bound the front door seat for {waited} minutes; the master fallback forces master up"
    return [Finding("master critical", slug, summary, (began, _last(state.get("attempts", []))), THRESHOLD, waited)]


def _watch_finding(store: Any, slug: str, now_ms: int) -> list[Finding]:
    raw = store.redis.get(store.key(slug, BEAT))
    if not raw:
        return []
    last = json.loads(raw)
    waited = (now_ms - last["at"]) // MINUTE_MS
    if last["error"]:
        summary = f"The master fallback watch of this swarm failed: {last['error']}"
    elif now_ms - last["at"] >= STALE_MS:
        summary = f"The master fallback watch of this swarm last ran {waited} minutes ago"
    else:
        return []
    evidence = (f"Last watch pass at {_clock(last['at'])}",)
    return [Finding("master watch", slug, summary, evidence, WATCH_THRESHOLD, waited)]


def _critical(store: Any, slug: str, state: dict[str, Any], now_ms: int, force: Callable[[str, int], int]) -> Change:
    lines: list[str] = []
    seat = ""
    if "critical_at" not in state:
        state["critical_at"] = now_ms
        seat = CRITICAL.format(slug=slug, minutes=(now_ms - state["since"]) // MINUTE_MS)
        _alert(state, seat)
        lines.append(f"swarm {slug} is critical: no bound front door master since {_clock(state['since'])}")
    attempts: list[dict[str, Any]] = state.setdefault("attempts", [])
    last = attempts[-1] if attempts else None
    if (last and now_ms - last["at"] < RETRY_MS) or _starting(store, slug, now_ms):
        return Change(lines, _still(slug, last) or seat)
    if last and last["outcome"] != FAILED:
        lines.append(_failed(slug, state, last, NO_BIND if last["outcome"] == STARTED else NO_OUTCOME, now_ms))
    state["attempts"] = [*attempts, {"at": now_ms, "outcome": "", "reason": ""}][-KEPT:]
    _save(store, slug, state)
    lines.append(_start_runner(slug, state, now_ms, force))
    return Change(lines, _still(slug, state["attempts"][-1]) or _still(slug, last) or seat)


def _still(slug: str, attempt: dict[str, Any] | None) -> str:
    """The seat alert text naming a failed forced start's reason, empty for no attempt or one that did not fail."""
    return SEAT_STILL.format(slug=slug, reason=attempt["reason"]) if attempt and attempt["outcome"] == FAILED else ""


def _start_runner(slug: str, state: dict[str, Any], now_ms: int, force: Callable[[str, int], int]) -> str:
    """Start the forced runner; a runner that cannot start fails its attempt at once with the launch error."""
    try:
        return f"forced master up of swarm {slug}: runner {force(slug, now_ms)}"
    except OSError as error:
        return _failed(slug, state, state["attempts"][-1], f"{type(error).__name__}: {error}", now_ms)


def _seat(ledger: Any, slug: str, change: Change) -> list[str]:
    """Write a watch pass's change to the ledger's front door seat alerts, once the record lock is released."""
    if change.close:
        return _close_seat_alerts(ledger, slug, change.close)
    return _seat_alert(ledger, slug, change.seat) if change.seat else []


def _seat_alert(ledger: Any, slug: str, text: str) -> list[str]:
    """Put text on the ledger's open front door seat alert. The ledger keeps one open alert per source and ignores a
    raise while one is open, so an open seat alert with other text is closed first and a new one raised in its place,
    and one that already carries text stays as it is; a ledger that cannot take it is named in the pass output,
    after any alert it closed."""
    closed: list[str] = []
    try:
        opened = _open_seat_alerts(ledger, slug)
        if any(alert.get("text") == text for alert in opened):
            return []
        closed = _close(ledger, slug, opened, REPLACED.format(text=text))
        ledger.raise_alert(slug, SEAT, text)
    except (SwarmError, OSError) as error:
        return [*closed, f"the ledger did not take the seat alert of swarm {slug}: {type(error).__name__}: {error}"]
    return [*closed, f"raised the seat alert of swarm {slug}"]


def _close_seat_alerts(ledger: Any, slug: str, outcome: str) -> list[str]:
    """Close every open front door seat alert on the ledger, as the night watch does once the seat is held."""
    try:
        return _close(ledger, slug, _open_seat_alerts(ledger, slug), outcome)
    except (SwarmError, OSError) as error:
        return [f"the ledger did not close the seat alert of swarm {slug}: {type(error).__name__}: {error}"]


def _open_seat_alerts(ledger: Any, slug: str) -> list[dict[str, Any]]:
    """The ledger's open front door seat alerts, read from its alerts alone rather than the whole ledger."""
    alerts: list[dict[str, Any]] = ledger.alerts(slug)
    return [alert for alert in alerts if alert.get("source") == SEAT and alert.get("state") != "done"]


def _close(ledger: Any, slug: str, opened: list[dict[str, Any]], outcome: str) -> list[str]:
    for alert in opened:
        ledger.close_alert(slug, alert["id"], outcome)
    return [f"closed the seat alert {alert['id']} of swarm {slug}" for alert in opened]


def _starting(store: Any, slug: str, now_ms: int) -> bool:
    """A front door master launched within its start deadline may still bind, so no forced start replaces it."""
    return any(
        agent.lane == MASTER
        and front_door.holds(slug, agent.seat)
        and agent.state != "finished"
        and now_ms - (agent.launched_at or agent.started_at) < master_start.deadline_ms(agent)
        for agent in store.agents(slug)
    )


def _failed(slug: str, state: dict[str, Any], attempt: dict[str, Any], reason: str, now_ms: int) -> str:
    attempt.update(outcome=FAILED, reason=reason, ended_at=now_ms)
    _alert(state, STILL.format(slug=slug, at=_clock(attempt["at"]), reason=reason))
    return f"forced master up of swarm {slug} at {_clock(attempt['at'])} failed: {reason}"


def _clear(store: Any, slug: str, state: dict[str, Any], why: str) -> Change:
    if not state:
        return Change([])
    store.redis.delete(store.key(slug, KEY))
    if "critical_at" not in state:
        return Change([])
    text = CLEARED.format(slug=slug, why=why)
    _alert(state, text, "resolved")
    return Change([text], close=text)


def _alert(state: dict[str, Any], text: str, severity: str = "critical") -> None:
    """Replace the swarm's standing broadcast, so one critical broadcast carries the latest state."""
    if state.get("alert"):
        broadcast.clear_broadcasts(message_id=state["alert"])
    state["alert"] = broadcast.create_broadcast(text, severity=severity, source=SOURCE)


def _last(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return "No forced master up has run yet"
    last = attempts[-1]
    outcome = f"{last['outcome']}: {last['reason']}" if last["reason"] else last["outcome"] or "running"
    return f"Last forced master up at {_clock(last['at'])}: {outcome}"


def _reason(output: str, code: int) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else f"master up exited with status {code}"


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _save(store: Any, slug: str, state: dict[str, Any]) -> None:
    store.redis.set(store.key(slug, KEY), json.dumps(state))
