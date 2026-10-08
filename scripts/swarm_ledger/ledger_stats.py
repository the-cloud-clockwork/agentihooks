"""The operator's stats check: code computes the review the orchestrator judges.

Pure functions over a ledger document and its `_meta` events.
"""

import math
import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ledger_core import Context

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
STALE_FLOOR_MINUTES = 15
DEFAULT_MINUTES = {"S": 10, "M": 25, "L": 40}
DEFAULT_DIFFICULTY = "M"
SAMPLES = 5
SAMPLE_WINDOW_MS = 24 * HOUR_MS
IN_FLIGHT = ("claimed", "pr")
UNOBSERVED = "live capacity has not been observed"
NO_SLOT = "the quota allows no agent slot now"


def live(doc, name):
    return [i for i in doc.get(name, []) if not i.get("out_of_scope")]


def finished(task):
    return task.get("state") == "done"


def stale_phases(doc):
    tasks = live(doc, "tasks")
    found = []
    for phase in live(doc, "phases"):
        mine = [t for t in tasks if t.get("phase") == phase["id"]]
        if not mine:
            continue
        unfinished = [t["id"] for t in mine if not finished(t)]
        if phase.get("done") and unfinished:
            found.append(f"{phase['id']} {phase['title']} is done with {', '.join(unfinished)} not done")
        elif not phase.get("done") and not unfinished:
            found.append(f"{phase['id']} {phase['title']} is open with every task done")
    return found


def undecided_followups(doc):
    return [f["text"] for f in live(doc, "followups") if not f.get("done")]


def task_counts(doc):
    tasks = live(doc, "tasks")
    return {state: sum(1 for t in tasks if t.get("state") == state) for state in ("open", "claimed", "pr")}


def closed_last_hour(doc, events, now):
    recent = {e["target"] for e in events if e["kind"] == "task done" and now - e["at"] <= HOUR_MS}
    return [t["id"] for t in live(doc, "tasks") if finished(t) and f"tasks/{t['id']}" in recent]


def difficulty(task):
    return task.get("difficulty") if task.get("difficulty") in DEFAULT_MINUTES else DEFAULT_DIFFICULTY


def claim_to_pr(doc, events, now):
    rows = {f"tasks/{t['id']}": t for t in live(doc, "tasks")}
    claimed, spans = {}, {name: [] for name in DEFAULT_MINUTES}
    for e in events:
        if e["kind"] == "task claimed":
            claimed[e["target"]] = e["at"]
        elif e["kind"] == "task pr" and e["target"] in claimed and e["target"] in rows:
            start = claimed.pop(e["target"])
            if now - e["at"] <= SAMPLE_WINDOW_MS:
                spans[difficulty(rows[e["target"]])].append((e["at"] - start) / MINUTE_MS)
    return spans


def agent_minutes(doc, events, now):
    spans = claim_to_pr(doc, events, now)
    return {
        name: {
            "minutes": round(statistics.median(found), 1) if len(found) >= SAMPLES else DEFAULT_MINUTES[name],
            "samples": len(found),
        }
        for name, found in spans.items()
    }


def remaining_minutes(task, tiers, ci, events, now):
    estimate = tiers[difficulty(task)]["minutes"] + ci
    if task.get("state") not in IN_FLIGHT:
        return estimate
    target, kind = f"tasks/{task['id']}", f"task {task['state']}"
    full = ci if task["state"] == "pr" else estimate
    since = [e["at"] for e in events if e["kind"] == kind and e["target"] == target]
    return max(0, full - (now - since[-1]) / MINUTE_MS) if since else full


def chain_minutes(unfinished, left):
    total = {}

    def walk(tid, stack):
        if tid in total:
            return total[tid]
        stack = stack | {tid}
        deps = [d for d in unfinished[tid].get("depends_on", []) if d in unfinished and d not in stack]
        total[tid] = left[tid] + max((walk(d, stack) for d in deps), default=0)
        return total[tid]

    return max((walk(tid, frozenset()) for tid in unfinished), default=0)


def is_stale(shown, computed):
    if computed is None:
        return False
    if shown is None:
        return True
    return abs(shown - computed) > max(STALE_FLOOR_MINUTES, computed / 4)


def calculate(doc: dict, events: list, now: int, inputs: dict | None = None) -> dict:
    slots, ci = (inputs or {}).get("slots"), (inputs or {}).get("ci_minutes")
    tiers = agent_minutes(doc, events, now)
    unfinished = {t["id"]: t for t in live(doc, "tasks") if not finished(t)}
    left = {tid: remaining_minutes(t, tiers, ci or 0, events, now) for tid, t in unfinished.items()}
    chain, work = chain_minutes(unfinished, left), sum(left.values())
    throughput = work / slots if slots else None
    if not unfinished:
        minutes, gap = 0, ""
    elif throughput is None:
        minutes, gap = None, NO_SLOT if slots == 0 else UNOBSERVED
    else:
        minutes, gap = math.ceil(max(chain, throughput)), ""
    return {
        "minutes": minutes,
        "remaining": len(unfinished),
        "work": round(work, 1),
        "chain": round(chain, 1),
        "throughput": None if throughput is None else round(throughput, 1),
        "slots": slots,
        "ci_minutes": ci,
        "tiers": tiers,
        "stale": is_stale(doc.get("time_left_minutes"), minutes),
        "gap": gap,
    }


def clock(minutes):
    return "not set" if minutes is None else f"{minutes // 60}h {minutes % 60}m"


def time_left_line(doc, events, now, calculation=None):
    result = calculate(doc, events, now) if calculation is None else calculation
    shown, computed = doc.get("time_left_minutes"), result["minutes"]
    if computed is None:
        return f"the page shows {clock(shown)}, code cannot compute it: {result['gap']}"
    verdict = "stale" if result["stale"] else "current"
    if not result["remaining"]:
        return f"the page shows {clock(shown)}, computed 0h 0m with no task remaining, {verdict}"
    tiers = ", ".join(f"{name} {entry['minutes']:g}m" for name, entry in result["tiers"].items())
    ci = "no CI median yet" if result["ci_minutes"] is None else f"{result['ci_minutes']:g}m of CI"
    return (
        f"the page shows {clock(shown)}, computed {clock(computed)} as the larger of a {result['chain']:g}m chain "
        f"and {result['work']:g}m of work over {result['slots']} slots, for {result['remaining']} remaining tasks "
        f"at {tiers} and {ci}, {verdict}"
    )


def listed(items):
    return "; ".join(i.rstrip(". ") for i in items) or "none"


def tick_inputs(meta):
    return (meta.get("time_left") or {}).get("inputs")


def review(doc, meta, now, calculation=None):
    from ledger_gate import plural

    events = meta.get("events", [])
    counts = task_counts(doc)
    rate = len(closed_last_hour(doc, events, now))
    if calculation is None:
        calculation = calculate(doc, events, now, tick_inputs(meta))
    return (
        "Operator stats check, computed now. "
        f"Stale phases: {listed(stale_phases(doc))}. "
        f"Undecided follow-ups: {listed(undecided_followups(doc))}. "
        f"Tasks: {counts['open']} open, {counts['claimed']} claimed, {counts['pr']} pr. "
        f"Close rate: {plural(rate, 'task')} in the last hour. "
        f"Time left: {time_left_line(doc, events, now, calculation)}. "
        "Judge stale phases and undecided follow-ups against the real work, then ack."
    )


def refresh(doc: dict, ctx: "Context", request_id: str) -> str:
    state = {"id": request_id, "rev": ctx.rev, "at": ctx.at}
    ctx.meta["stats_refresh"] = state
    events = ctx.meta["events"] + ctx.events
    try:
        result = calculate(doc, events, ctx.at, tick_inputs(ctx.meta))
        text = review(doc, {"events": events}, ctx.at, result)
        counts = {
            name: {
                "done": sum(bool(item.get("done")) or finished(item) for item in live(doc, name)),
                "total": len(live(doc, name)),
            }
            for name in ("phases", "tasks", "followups")
        }
    except Exception as exc:
        error = f"Stats calculation failed: {type(exc).__name__}"
        state.update(state="failed", completed_at=ctx.at, error=error)
        ctx.record("stats", "stats refresh failed", "", id=request_id)
        return f"{error}. Prior estimate retained; retry the refresh. Judge open follow-ups separately."
    if result["minutes"] is not None:
        doc["time_left_minutes"] = result["minutes"]
        ctx.stamp("time_left_minutes", "stats")
    state.update(state="refreshed", completed_at=ctx.at, counts=counts, calculation=result)
    ctx.record("stats", "stats refreshed", "", id=request_id)
    return text
