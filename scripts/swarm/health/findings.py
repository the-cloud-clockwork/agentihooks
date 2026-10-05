"""Swarm health: ledger events, the agent registry and tool activity in, coordination-failure findings out.

The calculator only reports; the master diagnoses and the operator decides.
"""

import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, fields

WORKER_RE = re.compile(r"-(eng|ci)-\d+$")
MASTER_RE = re.compile(r"-master-\d+$")
NOT_TRANSITIONS = ("joined", "left")
HELD = ("claimed", "pr")
MINUTE_MS = 60_000
ENV = {
    "ceremony_min": "AGENTIHOOKS_HEALTH_CEREMONY_MIN",
    "ceremony_ratio": "AGENTIHOOKS_HEALTH_CEREMONY_RATIO",
    "self_queued": "AGENTIHOOKS_HEALTH_SELF_QUEUED",
    "reruns": "AGENTIHOOKS_HEALTH_RERUNS",
    "review_rounds": "AGENTIHOOKS_HEALTH_REVIEW_ROUNDS",
    "idle_ticks": "AGENTIHOOKS_HEALTH_IDLE_TICKS",
    "stale_minutes": "AGENTIHOOKS_HEALTH_STALE_MINUTES",
    "watch_min": "AGENTIHOOKS_HEALTH_WATCH_MIN",
    "watch_ratio": "AGENTIHOOKS_HEALTH_WATCH_RATIO",
}


@dataclass(frozen=True)
class Limits:
    ceremony_min: int = 20
    ceremony_ratio: int = 12
    self_queued: int = 3
    reruns: int = 2
    review_rounds: int = 3
    idle_ticks: int = 3
    stale_minutes: int = 30
    watch_min: int = 20
    watch_ratio: int = 5


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    evidence: str
    threshold: str

    def as_dict(self):
        return asdict(self)


def limits(environ=None):
    env = os.environ if environ is None else environ
    values = {}
    for field in fields(Limits):
        raw = env.get(ENV[field.name], "")
        if raw.isdigit() and int(raw) > 0:
            values[field.name] = int(raw)
    return Limits(**values)


def findings(ledger, agents, activity, now_ms, limits):
    events = ledger.get("_meta", {}).get("events", [])
    tasks = {t["id"]: t for t in ledger.get("tasks", [])}
    return [
        *ceremony(events, tasks, limits),
        *scope_inflation(events, tasks, limits),
        *proof_loops(events, tasks, limits),
        *idle_with_claim(agents, tasks, limits),
        *stale_claims(events, tasks, now_ms, limits),
        *over_monitoring(activity, limits),
    ]


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _task_id(target):
    return target.split("/", 1)[1] if target.startswith("tasks/") else ""


def ceremony(events, tasks, limits):
    merged = {tid for tid, t in tasks.items() if t.get("state") == "done" and t.get("pr_url")}
    moves = Counter(e["by"] for e in events if e.get("kind") not in NOT_TRANSITIONS)
    closed = Counter(e["by"] for e in events if e.get("kind") == "task done" and _task_id(e["target"]) in merged)
    found = []
    for by, count in sorted(moves.items()):
        if not (WORKER_RE.search(by) or MASTER_RE.search(by)):
            continue
        outcomes = len(merged) if MASTER_RE.search(by) else closed[by]
        if count >= limits.ceremony_min and count / max(outcomes, 1) > limits.ceremony_ratio:
            found.append(
                Finding(
                    "ceremony",
                    by,
                    f"{count} ledger transitions against {_plural(outcomes, 'merged outcome')}",
                    f"at least {limits.ceremony_min} transitions and more than {limits.ceremony_ratio} per outcome",
                )
            )
    return found


def _gain(task):
    gain = task.get("gain")
    return gain if isinstance(gain, (int, float)) and not isinstance(gain, bool) else None


def scope_inflation(events, tasks, limits):
    queued = {}
    for e in events:
        tid = _task_id(e.get("target", ""))
        if e.get("kind") == "added" and WORKER_RE.search(e["by"]) and tid in tasks:
            queued.setdefault(e["by"], []).append(tid)
    found = []
    for by, ids in sorted(queued.items()):
        gains = [_gain(tasks[tid]) or 0 for tid in ids]
        if len(ids) < limits.self_queued or any(b > a for a, b in zip(gains, gains[1:])):
            continue
        listed = ", ".join(
            f"{tid} gain {_gain(tasks[tid]):g}" if _gain(tasks[tid]) is not None else f"{tid} no gain stated"
            for tid in ids
        )
        found.append(
            Finding(
                "scope inflation",
                by,
                f"queued {len(ids)} tasks for its own lane: {listed}",
                f"{limits.self_queued} self queued tasks whose gain never rose",
            )
        )
    return found


def proof_loops(events, tasks, limits):
    claims = Counter(_task_id(e["target"]) for e in events if e.get("kind") == "task claimed")
    rounds = Counter(_task_id(e["target"]) for e in events if e.get("kind") == "task pr")
    found = []
    for tid in tasks:
        reruns = max(claims[tid] - 1, 0)
        if reruns > limits.reruns or rounds[tid] > limits.review_rounds:
            found.append(
                Finding(
                    "proof loop",
                    tid,
                    f"claimed {_plural(claims[tid], 'time')} ({_plural(reruns, 'rerun')}), "
                    f"{_plural(rounds[tid], 'review round')}",
                    f"more than {limits.reruns} reruns or {limits.review_rounds} review rounds",
                )
            )
    return found


def idle_with_claim(agents, tasks, limits):
    found = []
    for a in agents:
        held = tasks.get(a.get("task"), {})
        if a.get("lane") == "master" or held.get("state") not in HELD or a.get("idle_ticks", 0) < limits.idle_ticks:
            continue
        found.append(
            Finding(
                "idle with claim",
                a["name"],
                f"idle for {a['idle_ticks']} ticks while holding task {held['id']} ({held['state']})",
                f"{limits.idle_ticks} idle ticks",
            )
        )
    return found


def stale_claims(events, tasks, now_ms, limits):
    found = []
    for tid, t in tasks.items():
        holder = t.get("claimed_by")
        if t.get("state") not in HELD or not holder:
            continue
        seen = [e["at"] for e in events if e.get("target") == f"tasks/{tid}" or e.get("by") == holder]
        quiet = (now_ms - max(seen)) // MINUTE_MS if seen else 0
        if quiet >= limits.stale_minutes:
            found.append(
                Finding(
                    "stale claim",
                    tid,
                    f"claimed by {holder}, no change for {_plural(quiet, 'minute')}",
                    f"{_plural(limits.stale_minutes, 'minute')} without a change",
                )
            )
    return found


def over_monitoring(activity, limits):
    found = []
    for by, counts in sorted(activity.items()):
        watch, act = counts.get("watch", 0), counts.get("act", 0)
        if watch >= limits.watch_min and watch / max(act, 1) > limits.watch_ratio:
            found.append(
                Finding(
                    "over monitoring",
                    by,
                    f"{watch} watch calls against {_plural(act, 'action')}",
                    f"at least {limits.watch_min} watch calls and more than {limits.watch_ratio} per action",
                )
            )
    return found
