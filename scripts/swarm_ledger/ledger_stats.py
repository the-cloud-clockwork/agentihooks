"""The operator's stats check: code computes the review the orchestrator judges.

Pure functions over a ledger document and its `_meta` events.
"""

import math

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
STALE_FLOOR_MINUTES = 15


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


def mean_minutes(events, task_ids):
    spans = []
    for tid in task_ids:
        target = f"tasks/{tid}"
        done = [e["at"] for e in events if e["kind"] == "task done" and e["target"] == target]
        claims = [e["at"] for e in events if e["kind"] == "task claimed" and e["target"] == target]
        if done and claims:
            spans.append(done[-1] - claims[-1])
    return round(sum(spans) / len(spans) / MINUTE_MS) if spans else None


def chain_length(doc):
    unfinished = {t["id"]: t for t in live(doc, "tasks") if not finished(t)}
    depth = {}

    def walk(tid, stack):
        if tid in depth:
            return depth[tid]
        stack = stack | {tid}
        deps = [d for d in unfinished[tid].get("depends_on", []) if d in unfinished and d not in stack]
        depth[tid] = 1 + max((walk(d, stack) for d in deps), default=0)
        return depth[tid]

    return max((walk(tid, frozenset()) for tid in unfinished), default=0)


def time_left(remaining, rate, chain, mean):
    if remaining == 0:
        return 0
    if rate == 0:
        return None
    bound = remaining * 60 / rate
    if mean is not None:
        bound = max(bound, chain * mean)
    return math.ceil(bound)


def is_stale(shown, computed):
    if computed is None:
        return False
    if shown is None:
        return True
    return abs(shown - computed) > max(STALE_FLOOR_MINUTES, computed / 4)


def clock(minutes):
    return "not set" if minutes is None else f"{minutes // 60}h {minutes % 60}m"


def time_left_line(doc, events, now):
    closed = closed_last_hour(doc, events, now)
    remaining = sum(1 for t in live(doc, "tasks") if not finished(t))
    chain, mean = chain_length(doc), mean_minutes(events, closed)
    shown = doc.get("time_left_minutes")
    computed = time_left(remaining, len(closed), chain, mean)
    if computed is None:
        return f"the page shows {clock(shown)}, no task closed in the last hour so code cannot compute it"
    per_task = "" if mean is None else f" at {mean}m a task"
    verdict = "stale" if is_stale(shown, computed) else "current"
    return (
        f"the page shows {clock(shown)}, computed {clock(computed)} from {remaining} remaining at "
        f"{len(closed)} an hour and a chain of {chain}{per_task}, {verdict}"
    )


def listed(items):
    return "; ".join(i.rstrip(". ") for i in items) or "none"


def review(doc, meta, now):
    from ledger_gate import plural

    events = meta.get("events", [])
    counts = task_counts(doc)
    rate = len(closed_last_hour(doc, events, now))
    return (
        "Operator stats check, computed now. "
        f"Stale phases: {listed(stale_phases(doc))}. "
        f"Undecided follow-ups: {listed(undecided_followups(doc))}. "
        f"Tasks: {counts['open']} open, {counts['claimed']} claimed, {counts['pr']} pr. "
        f"Close rate: {plural(rate, 'task')} in the last hour. "
        f"Time left: {time_left_line(doc, events, now)}. "
        "Judge each line against the real work, fix what is stale, then ack."
    )
