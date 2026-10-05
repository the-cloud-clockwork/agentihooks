"""Findings over a swarm's Langfuse traces: tool error rate, cost per merged task, gaps between turns, untraced sessions."""

from collections import Counter
from dataclasses import dataclass, fields
from statistics import median

from scripts.swarm.health.findings import Finding

MINUTE_MS = 60_000


@dataclass(frozen=True)
class Limits:
    tool_error_pct: int = 20
    tool_error_min_calls: int = 10
    task_cost_ratio: int = 2
    turn_gap_minutes: int = 30
    grace_minutes: int = 30

    @classmethod
    def from_env(cls, environ):
        values = {}
        for field in fields(cls):
            raw = environ.get(f"AGENTIHOOKS_DOCTOR_{field.name.upper()}", "")
            if raw.isdigit() and int(raw) > 0:
                values[field.name] = int(raw)
        return cls(**values)


def _agent(trace):
    return next((t[len("agent:") :] for t in trace["tags"] if t.startswith("agent:")), trace["name"])


def _subject(trace):
    return trace["session_id"] or trace["id"]


def _of(trace, kind):
    return [o for o in trace["observations"] if o["type"] == kind]


def _turns(trace):
    spans = {o["turn"]: o for o in _of(trace, "SPAN") if o["turn"] is not None}
    return [spans[n] for n in sorted(spans)]


def _gaps(trace):
    turns = _turns(trace)
    return [(a["turn"], b["turn"], (b["start"] - a["end"]) // MINUTE_MS) for a, b in zip(turns, turns[1:])]


def tool_errors(record: dict, limits: Limits) -> list[Finding]:
    found = []
    for trace in record["traces"]:
        tools = _of(trace, "TOOL")
        failed = Counter(o["name"] for o in tools if o["error"])
        errors = sum(failed.values())
        if len(tools) < limits.tool_error_min_calls or errors * 100 <= limits.tool_error_pct * len(tools):
            continue
        found.append(
            Finding(
                "tool errors",
                _subject(trace),
                f"{errors} of {len(tools)} tool calls failed",
                (
                    f"agent {_agent(trace)}",
                    f"{errors}/{len(tools)} tool calls failed",
                    "failed tools: " + ", ".join(f"{name} {n}" for name, n in failed.most_common()),
                    f"trace {trace['id']}",
                ),
                f"over {limits.tool_error_pct}% of at least {limits.tool_error_min_calls} tool calls",
                round(errors * 100 / len(tools)),
            )
        )
    return found


def _task_costs(record):
    costs = []
    for task in record["merged"]:
        traced = [t for t in record["traces"] if f"task:{task}" in t["tags"]]
        costs.append(
            {
                "task": task,
                "sessions": len(traced),
                "turns": sum(len(_turns(t)) for t in traced),
                "tokens": sum(o["tokens"] for t in traced for o in _of(t, "GENERATION")),
            }
        )
    return costs


def task_cost(record: dict, limits: Limits) -> list[Finding]:
    costs = [c for c in _task_costs(record) if c["tokens"]]
    found = []
    for cost in costs:
        others = [c["tokens"] for c in costs if c is not cost]
        if not others or cost["tokens"] <= limits.task_cost_ratio * median(others):
            continue
        found.append(
            Finding(
                "task cost",
                cost["task"],
                f"{cost['tokens']} tokens, over {limits.task_cost_ratio} times the other merged tasks",
                (
                    f"turns {cost['turns']}",
                    f"traced sessions {cost['sessions']}",
                    f"tokens {cost['tokens']}, median of the other merged tasks {round(median(others))}",
                ),
                f"over {limits.task_cost_ratio} times the median tokens of the other merged tasks",
                cost["tokens"],
            )
        )
    return found


def turn_gaps(record: dict, limits: Limits) -> list[Finding]:
    found = []
    for trace in record["traces"]:
        long = [g for g in _gaps(trace) if g[2] > limits.turn_gap_minutes]
        if not long:
            continue
        found.append(
            Finding(
                "turn gap",
                _subject(trace),
                f"{len(long)} gaps between turns over {limits.turn_gap_minutes} minutes",
                (f"agent {_agent(trace)}", *(f"turn {a} to turn {b}: {m} minutes" for a, b, m in long)),
                f"over {limits.turn_gap_minutes} minutes between turns",
                max(m for _, _, m in long),
            )
        )
    return found


def untraced(record: dict, limits: Limits) -> list[Finding]:
    agents = {_agent(t) for t in record["traces"]}
    session_ids = {t["session_id"] for t in record["traces"] if t["session_id"]}
    found = []
    for session in record["sessions"]:
        if session["agent"] in agents or session["session_id"] in session_ids:
            continue
        if session["started_at"] and record["now_ms"] - session["started_at"] < limits.grace_minutes * MINUTE_MS:
            continue
        found.append(
            Finding(
                "untraced session",
                session["agent"],
                "swarm session with no Langfuse trace",
                (
                    f"task {session['task']}",
                    f"session {session['session_id'] or 'unknown'}",
                    f"no trace tagged agent:{session['agent']} in swarm {record['slug']}",
                ),
                "no trace for a session past its grace period",
                1,
            )
        )
    return found


def measures(record: dict) -> dict:
    return {
        "tasks": _task_costs(record),
        "sessions": [
            {
                "agent": _agent(t),
                "session": _subject(t),
                "tool_calls": len(_of(t, "TOOL")),
                "tool_errors": sum(1 for o in _of(t, "TOOL") if o["error"]),
                "turns": len(_turns(t)),
                "longest_gap_minutes": max((m for _, _, m in _gaps(t)), default=0),
                "tokens": sum(o["tokens"] for o in _of(t, "GENERATION")),
            }
            for t in record["traces"]
        ],
    }


def findings(record: dict, limits: Limits) -> list[Finding]:
    return [
        *tool_errors(record, limits),
        *task_cost(record, limits),
        *turn_gaps(record, limits),
        *untraced(record, limits),
    ]
