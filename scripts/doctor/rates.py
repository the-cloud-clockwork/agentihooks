"""Doctor rates: each coordination failure's number over a time window, and the gate log's counts in it.

Every gate measures its failure before and after it ships with the same functions, over records read once.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean

from scripts.swarm import naming
from scripts.swarm.health import activity as health_activity
from scripts.swarm_ledger import ledger_kinds

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
TALK = ("comment added", "comment edited", "message added")
OUTCOMES = ("task pr", "task done")
GATE_KINDS = ("deny", "observe", "lift", "fail-open")
MERGED = "MERGED"


@dataclass(frozen=True)
class Window:
    start: int
    end: int

    def holds(self, at):
        return self.start <= at < self.end


@dataclass(frozen=True)
class Pull:
    state: str
    merged_at: int | None
    lines: int
    files: tuple


@dataclass(frozen=True)
class Records:
    events: list
    tasks: dict
    activity: dict
    findings: dict
    gate_log: list
    injections: list
    corrections: list
    pulls: dict


def ratio(part, whole):
    return round(part / whole, 2) if whole else None


def _average(values):
    return round(mean(values), 2) if values else None


def _in(records, window, *kinds):
    return [e for e in records.events if e.get("kind") in kinds and window.holds(e.get("at", -1))]


def _task(event):
    target = event.get("target", "")
    return target.split("/", 1)[1] if target.startswith("tasks/") else ""


def _worker(by):
    return naming.lane_of(by) in ("eng", "ci")


def _talk(event):
    followup = event.get("kind") == "added" and event.get("target", "").startswith("followups/")
    return event.get("kind") in TALK or followup


def _done(records, window):
    return {_task(e): e["at"] for e in _in(records, window, "task done") if _task(e) in records.tasks}


def _pull(records, task_id):
    return records.pulls.get(records.tasks[task_id].get("pr_url") or "")


def ceremony(records, window):
    talk = [e for e in _in(records, window, *TALK, "added") if _talk(e) and _worker(e.get("by", ""))]
    outcomes = len(_in(records, window, *OUTCOMES))
    return {"talk writes": len(talk), "outcomes": outcomes, "talk per outcome": ratio(len(talk), outcomes)}


def _outside(files, territory):
    return sum(1 for f in files if not any(f == t or f.startswith(t.rstrip("/") + "/") for t in territory))


def scope_inflation(records, window):
    merged = [tid for tid in _done(records, window) if getattr(_pull(records, tid), "state", "") == MERGED]
    bounded = [tid for tid in merged if records.tasks[tid].get("territory")]
    outside = sum(_outside(_pull(records, tid).files, records.tasks[tid]["territory"]) for tid in bounded)
    added = [e for e in _in(records, window, "added") if _task(e) and _worker(e.get("by", ""))]
    return {
        "merged tasks": len(merged),
        "lines per merged task": _average([_pull(records, tid).lines for tid in merged]),
        "files outside territory per merged task": ratio(outside, len(bounded)),
        "tasks added by workers": len(added),
    }


def context_narrowing(records, window):
    reached = {}
    for e in records.events:
        if e.get("kind") in OUTCOMES:
            reached.setdefault(_task(e), e["at"])
    opened = [e for e in _in(records, window, "task open") if reached.get(_task(e), e["at"]) < e["at"]]
    done = len(_in(records, window, "task done"))
    return {"tasks reopened": len(opened), "tasks done": done, "reopened per done": ratio(len(opened), done)}


def proof_loops(records, window):
    claims, moves = _in(records, window, "task claimed"), _in(records, window, "task pr")
    tasks = {_task(e) for e in (*claims, *moves)}
    return {
        "tasks claimed": len({_task(e) for e in claims}),
        "claims per task": ratio(len(claims), len(tasks)),
        "pr moves per task": ratio(len(moves), len(tasks)),
    }


def inherited_rules(records, window):
    first = {}
    for c in sorted(records.corrections, key=lambda c: c["at"]):
        first.setdefault(c["source"], c["at"])
    late = [i for i in records.injections if window.holds(i["at"]) and first.get(i["source"], i["at"]) < i["at"]]
    twice = Counter(c["source"] for c in records.corrections if c["at"] < window.end)
    return {
        "corrections": sum(1 for c in records.corrections if window.holds(c["at"])),
        "injections after correction": len(late),
        "sources corrected twice": sum(1 for n in twice.values() if n >= 2),
    }


def monitoring(records, window):
    workers, master = {"watch": 0, "act": 0}, {"watch": 0, "act": 0}
    for name, rows in records.activity.items():
        lane = naming.lane_of(name)
        if lane not in ("eng", "ci", "master"):
            continue
        counted = health_activity.tally([r for r in rows if window.holds(r.get("at", -1))])
        into = master if lane == "master" else workers
        for kind in into:
            into[kind] += counted[kind]
    return {
        "worker watch calls": workers["watch"],
        "worker actions": workers["act"],
        "worker watch per action": ratio(workers["watch"], workers["act"]),
        "master watch per action": ratio(master["watch"], master["act"]),
    }


def _findings(records, window, kind):
    return [r for fid, r in records.findings.items() if fid.startswith(kind + "/") and window.holds(r["seen_at"])]


def idle(records, window):
    waits = []
    for tid, at in _done(records, window).items():
        pull = _pull(records, tid)
        if pull and pull.merged_at:
            waits.append((at - pull.merged_at) / MINUTE_MS)
    return {
        "idle findings": len(_findings(records, window, "idle-with-claim")),
        "minutes from merge to done": _average(waits),
    }


def stale(records, window):
    found = _findings(records, window, "stale-claim")
    return {"stale findings": len(found), "quiet minutes per finding": _average([r["measure"] for r in found])}


def premature(records, window):
    code = [tid for tid in _done(records, window) if ledger_kinds.kind(records.tasks[tid]) in ("code", "ci")]
    unread = [tid for tid in code if records.tasks[tid].get("pr_url") and _pull(records, tid) is None]
    unmerged = [tid for tid in code if tid not in unread and getattr(_pull(records, tid), "state", "") != MERGED]
    return {
        "code tasks done": len(code),
        "done without a merged pull request": len(unmerged),
        "pull requests unread": len(unread),
    }


FAILURES = {
    "ceremony": ceremony,
    "scope inflation": scope_inflation,
    "context narrowing": context_narrowing,
    "proof loops": proof_loops,
    "inherited bad rules": inherited_rules,
    "hyper monitoring": monitoring,
    "idle with claim": idle,
    "stale claim": stale,
    "premature completion": premature,
}


def rates(records, window):
    return {name: measure(records, window) for name, measure in FAILURES.items()}


def gate_counts(records, window):
    found = {}
    for row in records.gate_log:
        if window.holds(row.get("at", -1)) and row.get("kind") in GATE_KINDS:
            found.setdefault(row.get("gate", ""), dict.fromkeys(GATE_KINDS, 0))[row["kind"]] += 1
    return found


def windows(now_ms, hours, at_ms=None):
    span = int(hours * HOUR_MS)
    if at_ms is None:
        return {"now": Window(now_ms - span, now_ms)}
    return {"before": Window(at_ms - span, at_ms), "after": Window(at_ms, min(at_ms + span, now_ms))}


def report(records, wins):
    return {
        "windows": {name: {"start": w.start, "end": w.end} for name, w in wins.items()},
        "events from": min((e["at"] for e in records.events), default=None),
        "rates": {name: rates(records, w) for name, w in wins.items()},
        "gates": {name: gate_counts(records, w) for name, w in wins.items()},
    }


def _when(ms):
    return "none" if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _cell(value):
    return "none" if value is None else str(value)


def table(found):
    names = list(found["windows"])
    lines = [f"window {n} {_when(w['start'])} to {_when(w['end'])}" for n, w in found["windows"].items()]
    lines.append(f"ledger events kept from {_when(found['events from'])}")
    rows = [("failure", "number", *names)]
    for failure, numbers in found["rates"][names[0]].items():
        rows += [(failure, number, *(_cell(found["rates"][n][failure][number]) for n in names)) for number in numbers]
    for gate in sorted({g for n in names for g in found["gates"][n]}):
        rows += [
            (f"gate {gate}", kind, *(str(found["gates"][n].get(gate, {}).get(kind, 0)) for n in names))
            for kind in GATE_KINDS
        ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join([*lines, "", *("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows)])
