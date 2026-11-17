"""Critical no master state, kept per swarm in its master-critical record and driven by the master fallback pass
outside the tick. A swarm whose front door seat has had no bound master for AGENTIHOOKS_MASTER_CRITICAL_MINUTES
(default 30) is critical: status shows it as a finding, a critical broadcast raises it, and the pass forces master up
once per retry window while no master start is in flight. Each forced start's outcome is kept with its time and
reason; a failed one, or one whose master never binds, raises the broadcast again. A bound front door master, or a
swarm that stops or sleeps with no work, clears it."""

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from hooks.context import broadcast
from scripts.swarm import lifetime, master_start
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import front_door

KEY = "master-critical"
MINUTES = "AGENTIHOOKS_MASTER_CRITICAL_MINUTES"
DEFAULT_MINUTES = 30.0
RETRY_MS = 5 * MINUTE_MS
KEPT = 10
QUIET = ("stopped", "stopping")
SOURCE = "swarm"
STARTED, FAILED = "started", "failed"
NO_OUTCOME = "the forced master up recorded no outcome within five minutes"
NO_BIND = "the master it started did not bind the front door seat within five minutes"
CRITICAL = (
    "CRITICAL: swarm {slug} has had no bound master for {minutes} minutes. The master fallback forces agentihooks "
    "swarm {slug} master up every five minutes until a master binds the front door seat."
)
STILL = "CRITICAL: swarm {slug} still has no master; the forced master up at {at} failed: {reason}"
CLEARED = "Swarm {slug} is out of critical mode: {why}."
THRESHOLD = "no bound front door master for AGENTIHOOKS_MASTER_CRITICAL_MINUTES, default thirty minutes"


def minutes(environ: Mapping[str, str]) -> float:
    """The minutes a running swarm may go with no bound front door master before it turns critical."""
    return float(environ.get(MINUTES) or DEFAULT_MINUTES)


def read(store: Any, slug: str) -> dict[str, Any]:
    """The swarm's critical record: the outage start, the critical time and the forced starts, empty when none."""
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def bound(store: Any, slug: str, live: set[str]) -> str:
    """The live working master on the front door seat, empty when the seat has none."""
    return next(
        (
            agent.name
            for agent in store.agents(slug)
            if agent.lane == MASTER and master_start.seated(agent, live, {}) and front_door.holds(slug, agent.seat)
        ),
        "",
    )


def watch(
    store: Any, slug: str, live: set[str], now_ms: int, environ: Mapping[str, str], force: Callable[[str, int], int]
) -> list[str]:
    """One watchdog pass over a swarm: note when its front door seat lost its bound master, turn critical at the
    threshold and force master up, and clear once a master binds or the swarm stops or sleeps."""
    state, master = read(store, slug), bound(store, slug, live)
    if master:
        return _clear(store, slug, state, f"master {master} bound the front door seat")
    sleeping = lifetime.sleeping(slug, store, {}, write=False)  # pyright: ignore[reportUnknownMemberType]
    if store.config(slug).state in QUIET or sleeping:
        return _clear(store, slug, state, "the swarm is stopped or sleeps with no work")
    state.setdefault("since", now_ms)
    lines = (
        _critical(store, slug, state, now_ms, force) if now_ms - state["since"] >= minutes(environ) * MINUTE_MS else []
    )
    _save(store, slug, state)
    return lines


def ended(store: Any, slug: str, at: int, code: int, output: str, now_ms: int) -> str:
    """Record how the forced master up started at `at` ended; a failure raises the critical broadcast again."""
    state = read(store, slug)
    attempt = next((row for row in state.get("attempts", []) if row["at"] == at), None)
    if attempt is None:
        return f"forced master up of swarm {slug} at {_clock(at)} ended after its critical state cleared"
    if code:
        line = _failed(slug, state, attempt, _reason(output, code), now_ms)
    else:
        attempt.update(outcome=STARTED, ended_at=now_ms)
        line = f"forced master up of swarm {slug} at {_clock(at)} started a master"
    _save(store, slug, state)
    return line


def findings(store: Any, slug: str, now_ms: int) -> list[Finding]:
    """The critical state as a swarm health finding, with when it began and how the last forced start went."""
    state = read(store, slug)
    if "critical_at" not in state:
        return []
    waited = (now_ms - state["since"]) // MINUTE_MS
    began = f"No bound master since {_clock(state['since'])}, critical since {_clock(state['critical_at'])}"
    summary = f"No master has bound the front door seat for {waited} minutes; the master fallback forces master up"
    return [Finding("master critical", slug, summary, (began, _last(state.get("attempts", []))), THRESHOLD, waited)]


def _critical(store: Any, slug: str, state: dict[str, Any], now_ms: int, force: Callable[[str, int], int]) -> list[str]:
    lines: list[str] = []
    if "critical_at" not in state:
        state["critical_at"] = now_ms
        _alert(state, CRITICAL.format(slug=slug, minutes=(now_ms - state["since"]) // MINUTE_MS))
        lines.append(f"swarm {slug} is critical: no bound front door master since {_clock(state['since'])}")
    attempts: list[dict[str, Any]] = state.setdefault("attempts", [])
    last = attempts[-1] if attempts else None
    if last and now_ms - last["at"] < RETRY_MS or _starting(store, slug, now_ms):
        return lines
    if last and last["outcome"] != FAILED:
        lines.append(_failed(slug, state, last, NO_BIND if last["outcome"] == STARTED else NO_OUTCOME, now_ms))
    state["attempts"] = [*attempts, {"at": now_ms, "outcome": "", "reason": ""}][-KEPT:]
    _save(store, slug, state)
    lines.append(f"forced master up of swarm {slug}: runner {force(slug, now_ms)}")
    return lines


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


def _clear(store: Any, slug: str, state: dict[str, Any], why: str) -> list[str]:
    if not state:
        return []
    store.redis.delete(store.key(slug, KEY))
    if "critical_at" not in state:
        return []
    _alert(state, CLEARED.format(slug=slug, why=why), "resolved")
    return [CLEARED.format(slug=slug, why=why)]


def _alert(state: dict[str, Any], text: str, severity: str = "critical") -> None:
    """Replace the swarm's standing broadcast, so one critical broadcast carries the latest state."""
    if state.get("alert"):
        broadcast.clear_broadcasts(message_id=state["alert"])
    state["alert"] = broadcast.create_broadcast(text, severity=severity, source=SOURCE) or ""


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
