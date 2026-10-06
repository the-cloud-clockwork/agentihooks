"""Swarm health: ledger events, the agent registry and tool activity in, coordination-failure findings out.

The calculator only reports; the master diagnoses and the operator decides.
"""

import os
from collections import Counter
from dataclasses import asdict, dataclass, fields

from scripts.swarm import naming
from scripts.swarm_ledger import ledger_kinds

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
    "master_watch_min": "AGENTIHOOKS_HEALTH_MASTER_WATCH_MIN",
    "master_watch_ratio": "AGENTIHOOKS_HEALTH_MASTER_WATCH_RATIO",
    "cooldown_minutes": "AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES",
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
    master_watch_min: int = 60
    master_watch_ratio: int = 15
    cooldown_minutes: int = 60


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    summary: str
    evidence: tuple
    threshold: str
    measure: int = 0

    @property
    def id(self):
        return f"{self.kind.replace(' ', '-')}/{self.subject}"

    def as_dict(self):
        found = asdict(self)
        del found["measure"]
        return {**found, "evidence": list(self.evidence)}


def limits(environ=None):
    env = os.environ if environ is None else environ
    values = {}
    for field in fields(Limits):
        raw = env.get(ENV[field.name], "")
        if raw.isdigit() and int(raw) > 0:
            values[field.name] = int(raw)
    return Limits(**values)


def findings(ledger, agents, activity, now_ms, limits, waiting=frozenset()):
    events = ledger.get("_meta", {}).get("events", [])
    tasks = {t["id"]: t for t in ledger.get("tasks", [])}
    return [
        *ceremony(events, tasks, limits),
        *scope_inflation(events, tasks, limits),
        *proof_loops(events, tasks, limits),
        *idle_with_claim(agents, tasks, limits, waiting),
        *waiting_on_input(agents, tasks),
        *stale_claims(events, tasks, now_ms, limits),
        *over_monitoring(activity, limits),
    ]


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _task_id(target):
    return target.split("/", 1)[1] if target.startswith("tasks/") else ""


def _is_master(by):
    return naming.lane_of(by) == "master"


def _is_worker(by):
    return naming.lane_of(by) in ("eng", "ci")


def _title(tasks, tid):
    return tasks.get(tid, {}).get("title") or tid


def _outcome(task):
    if task.get("state") != "done":
        return False
    return bool(task.get("pr_url")) or (ledger_kinds.kind(task) in ledger_kinds.NEEDS and not ledger_kinds.unmet(task))


def ceremony(events, tasks, limits):
    finished = {tid for tid, t in tasks.items() if _outcome(t)}
    moves = Counter(e["by"] for e in events if e.get("kind") not in NOT_TRANSITIONS)
    closed = Counter(e["by"] for e in events if e.get("kind") == "task done" and _task_id(e["target"]) in finished)
    found = []
    for by, count in sorted(moves.items()):
        if not naming.lane_of(by):
            continue
        outcomes = len(finished) if _is_master(by) else closed[by]
        if count >= limits.ceremony_min and count / max(outcomes, 1) > limits.ceremony_ratio:
            found.append(
                Finding(
                    "ceremony",
                    by,
                    "more ledger transitions than outcomes",
                    (f"{count} ledger transitions", _plural(outcomes, "outcome")),
                    f"at least {limits.ceremony_min} transitions and more than {limits.ceremony_ratio} per outcome",
                    count,
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
        if e.get("kind") == "added" and _is_worker(e["by"]) and tid in tasks:
            queued.setdefault(e["by"], []).append(tid)
    found = []
    for by, ids in sorted(queued.items()):
        gains = [_gain(tasks[tid]) or 0 for tid in ids]
        if len(ids) < limits.self_queued or any(b > a for a, b in zip(gains, gains[1:])):
            continue
        listed = tuple(
            f"{_title(tasks, tid)}, gain {_gain(tasks[tid]):g}"
            if _gain(tasks[tid]) is not None
            else f"{_title(tasks, tid)}, no gain stated"
            for tid in ids
        )
        found.append(
            Finding(
                "scope inflation",
                by,
                f"queued {len(ids)} tasks for its own lane",
                listed,
                f"{limits.self_queued} self queued tasks whose gain never rose",
                len(ids),
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
                    "reruns or review rounds over the cap",
                    (
                        f"task {_title(tasks, tid)}",
                        f"claimed {_plural(claims[tid], 'time')} ({_plural(reruns, 'rerun')})",
                        _plural(rounds[tid], "review round"),
                    ),
                    f"more than {limits.reruns} reruns or {limits.review_rounds} review rounds",
                    reruns + rounds[tid],
                )
            )
    return found


def idle_with_claim(agents, tasks, limits, waiting=frozenset()):
    found = []
    for a in agents:
        held = tasks.get(a.get("task"), {})
        if a.get("lane") == "master" or held.get("state") not in HELD or a.get("idle_ticks", 0) < limits.idle_ticks:
            continue
        if held["id"] in waiting:
            continue
        found.append(
            Finding(
                "idle with claim",
                a["name"],
                f"idle for {a['idle_ticks']} ticks while holding a task",
                (f"task {_title(tasks, held['id'])} ({held['state']})",),
                f"{limits.idle_ticks} idle ticks",
                a["idle_ticks"],
            )
        )
    return found


def waiting_on_input(agents: list[dict], tasks: dict[str, dict]) -> list[Finding]:
    found = []
    for agent in agents:
        held = tasks.get(agent.get("task"), {})
        ticks = agent.get("input_ticks", 0)
        if held.get("state") not in HELD or ticks <= 3 or not agent.get("input_prompt"):
            continue
        found.append(
            Finding(
                "waiting on input",
                agent["name"],
                f"waiting on input for {ticks} ticks while holding a task",
                (f"prompt: {agent['input_prompt']}", f"task {_title(tasks, held['id'])} ({held['state']})"),
                "more than 3 consecutive ticks waiting on input",
                ticks,
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
                    f"no change for {_plural(quiet, 'minute')}",
                    (f"task {_title(tasks, tid)}", f"claimed by {holder}"),
                    f"{_plural(limits.stale_minutes, 'minute')} without a change",
                    quiet,
                )
            )
    return found


def over_monitoring(activity, limits):
    found = []
    for by, counts in sorted(activity.items()):
        watch, act = counts.get("watch", 0), counts.get("act", 0)
        if _is_master(by):
            least, ratio = limits.master_watch_min, limits.master_watch_ratio
        else:
            least, ratio = limits.watch_min, limits.watch_ratio
        if watch >= least and watch / max(act, 1) > ratio:
            found.append(
                Finding(
                    "over monitoring",
                    by,
                    "more watch calls than actions",
                    (f"{watch} watch calls", _plural(act, "action")),
                    f"at least {least} watch calls and more than {ratio} per action",
                    watch,
                )
            )
    return found
