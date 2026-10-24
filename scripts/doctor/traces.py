"""Findings over a swarm's Langfuse traces: active telemetry freshness and attribution, reader coverage, tool error
rate, cost per merged task, gaps between turns, untraced sessions."""

from collections import Counter
from dataclasses import dataclass, fields
from statistics import median

from scripts.doctor.words import plural
from scripts.swarm.health.findings import Finding

MINUTE_MS = 60_000
SECOND_MS = 1000
BOUND_FIELDS = ("life", "seat", "task", "harness", "profile")
FAILURE_DETAIL = 50


@dataclass(frozen=True)
class Limits:
    tool_error_pct: int = 20
    tool_error_min_calls: int = 10
    task_cost_ratio: int = 2
    turn_gap_minutes: int = 30
    grace_minutes: int = 30
    active_stale_seconds: int = 60
    active_grace_seconds: int = 60
    interval_minutes: int = 10

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


def _partial(trace):
    return () if trace.get("complete", True) else ("observations read only up to where the read stopped",)


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
                    *_partial(trace),
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
        if any(not t.get("complete", True) or not _turns(t) for t in traced):
            continue
        costs.append(
            {
                "task": task,
                "sessions": len(traced),
                "turns": sum(len(_turns(t)) for t in traced),
                "tokens": sum(o["tokens"] for t in traced for o in _of(t, "GENERATION")),
            }
        )
    return costs


def _finished(record, task):
    traced = [t for t in record["traces"] if f"task:{task}" in t["tags"]]
    return max(o.get("end", 0) for t in traced for o in _turns(t))


def task_cost(record: dict, limits: Limits) -> list[Finding]:
    costs = [c for c in _task_costs(record) if c["tokens"]]
    finished = {c["task"]: _finished(record, c["task"]) for c in costs}
    found = []
    for cost in costs:
        if cost["task"] not in record.get("task_cost_candidates", record["merged"]):
            continue
        others = [c["tokens"] for c in costs if c is not cost and finished[c["task"]] <= finished[cost["task"]]]
        if not others or cost["tokens"] <= limits.task_cost_ratio * median(others):
            continue
        found.append(
            Finding(
                "task cost",
                cost["task"],
                f"{cost['tokens']} uncached tokens, over {limits.task_cost_ratio} times the other merged tasks",
                (
                    f"turns {cost['turns']}",
                    f"traced sessions {cost['sessions']}",
                    f"uncached tokens {cost['tokens']}, median of the other merged tasks {round(median(others))}",
                    "uncached tokens are input, cache writes and output; cache reads are left out",
                    *(
                        ("comparison set truncated: trace listing incomplete",)
                        if not record.get("reader", {}).get("historical", {}).get("listed", True)
                        else ()
                    ),
                ),
                f"over {limits.task_cost_ratio} times the median uncached tokens of the other merged tasks",
                cost["tokens"],
            )
        )
    return found


def _uncovered_ms(waits, start, end):
    cursor, uncovered = start, 0
    for held in sorted(waits, key=lambda held: held["at"]):
        left, right = max(cursor, held["at"]), min(end, held["until"])
        if right > left:
            uncovered += left - cursor
            cursor = right
    return uncovered + end - cursor


def _long_gaps(trace, limit):
    turns = _turns(trace)
    return [(a, b) for a, b in zip(turns, turns[1:]) if (b["start"] - a["end"]) // MINUTE_MS > limit]


def _uncovered_gaps(trace, waits, limit):
    found = []
    for a, b in _long_gaps(trace, limit):
        minutes = _uncovered_ms(waits, a["end"], b["start"]) // MINUTE_MS
        if minutes > limit:
            found.append((a["turn"], b["turn"], minutes, a["end"], b["start"]))
    return found


def turn_gaps(record: dict, limits: Limits) -> list[Finding]:
    found = []
    unread = set(record.get("waits_unread", []))
    for trace in record["traces"]:
        if _agent(trace) in unread:
            continue
        waits = record.get("waits", {}).get(_agent(trace), [])
        long = _uncovered_gaps(trace, waits, limits.turn_gap_minutes)
        if not long:
            continue
        found.append(
            Finding(
                "turn gap",
                _subject(trace),
                f"{len(long)} gaps between turns with over {limits.turn_gap_minutes} minutes outside declared waits",
                (
                    f"agent {_agent(trace)}",
                    *(
                        f"turn {a} to turn {b}: {m} minutes outside declared waits; gap {start} to {end}"
                        for a, b, m, start, end in long
                    ),
                    *_partial(trace),
                ),
                f"over {limits.turn_gap_minutes} minutes between turns outside declared waits",
                max(gap[2] for gap in long),
            )
        )
    return found


def _live(record):
    return {binding["agent"] for binding in record.get("active", [])}


def gap_agents(record: dict, limits: Limits) -> list[str]:
    newest = {}
    for trace in record["traces"]:
        for _, b in _long_gaps(trace, limits.turn_gap_minutes):
            newest[_agent(trace)] = max(newest.get(_agent(trace), b["start"]), b["start"])
    live = _live(record)
    return sorted(newest, key=lambda name: (name not in live, -newest[name], name))


def wait_coverage(record: dict, limits: Limits) -> list[Finding]:
    unread = record.get("waits_unread")
    if not unread:
        return []
    return [
        Finding(
            "wait history partial",
            record["slug"],
            f"wait history of {plural(len(unread), 'agent')} with turn gaps over {limits.turn_gap_minutes} minutes "
            "not read in budget",
            (
                "unread: " + ", ".join(unread[:5]),
                f"live agents unread: {len(_live(record) & set(unread))}",
                "turn gap findings skip agents whose wait history was not read",
                "each Doctor scan reads the wait history again within its budget",
            ),
            "every agent with a turn gap over the limit has its wait history read",
            len(unread),
        )
    ]


def untraced(record: dict, limits: Limits) -> list[Finding]:
    if not record.get("reader", {}).get("historical", {}).get("listed", True):
        return []
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


def _ago(now_ms, at_ms):
    return (now_ms - at_ms) // SECOND_MS


def _who(binding):
    return (
        f"agent {binding['agent']}",
        f"seat {binding['seat'] or 'unknown'}",
        f"task {binding['task'] or 'unknown'}",
        f"session {binding['session_id'] or 'unknown'}",
    )


def _cadence(limits):
    return f"judged at each Doctor scan, every {limits.interval_minutes} minutes"


def _remote_fresh(binding, now_ms):
    remote = binding["remote"]
    if not binding["read"]:
        return "Langfuse not read this scan"
    if not remote or not remote["fresh_ms"]:
        return "Langfuse shows no accepted event"
    return f"Langfuse last accepted event {_ago(now_ms, remote['fresh_ms'])} seconds ago"


def unaccepted_seconds(binding, now_ms):
    local = binding["local"]
    if not local or local["generated_bytes"] <= local["accepted_bytes"]:
        return 0
    return _ago(now_ms, local["oldest_unaccepted"])


def _stale(binding, record, limits, lag):
    local, now = binding["local"], record["now_ms"]
    queued = local["pending"] or local["overflow"]
    progress = (
        f"oldest unaccepted source event {lag} seconds old",
        f"generated {local['generated_bytes']} bytes, accepted {local['accepted_bytes']} bytes",
        f"{local['pending']} observations queued",
        *((f"pending cap overflow at {local['overflow']} bytes",) if local["overflow"] else ()),
        "exporter process alive" if local["exporter_alive"] else "no exporter process",
        f"last hook request {_ago(now, local['requested_at'])} seconds ago"
        if local["requested_at"]
        else "no hook request recorded",
        _remote_fresh(binding, now),
    )
    return Finding(
        "exporter backlog" if queued else "telemetry stale",
        f"{binding['agent']}.{local['oldest_unaccepted']}",
        f"source events {'queued' if queued else 'generated'} but unaccepted for {lag} seconds",
        (*_who(binding), *progress),
        f"oldest unaccepted source event older than {limits.active_stale_seconds} seconds, {_cadence(limits)}",
        lag,
    )


def _unconfirmed(binding, record, limits):
    local, remote, now = binding["local"], binding["remote"], record["now_ms"]
    if not (binding["read"] and local and remote) or local["accepted"] <= remote["observations"]:
        return None
    waited = _ago(now, local["accepted_at"])
    if waited <= limits.active_stale_seconds:
        return None
    return Finding(
        "telemetry stale",
        f"{binding['agent']}.{remote['fresh_ms']}",
        f"accepted observations missing from Langfuse {waited} seconds after acceptance",
        (
            *_who(binding),
            f"exporter recorded {local['accepted']} accepted observations, Langfuse holds {remote['observations']}",
            f"last acceptance {waited} seconds ago",
            _remote_fresh(binding, now),
        ),
        f"accepted observations absent from Langfuse past {limits.active_stale_seconds} seconds, {_cadence(limits)}",
        waited,
    )


def _never_exported(binding, record, limits):
    age = _ago(record["now_ms"], binding["started_at"])
    return Finding(
        "telemetry never exported",
        f"{binding['agent']}.{binding['started_at']}",
        f"working agent with no Langfuse trace after {age} seconds",
        (*_who(binding), f"no trace tagged swarm:{record['slug']} and agent:{binding['agent']}"),
        f"no trace after a {limits.active_grace_seconds} second grace, {_cadence(limits)}",
        age,
    )


def _misattributed(binding):
    mismatched = [
        (t, name)
        for t in binding["traces"]
        for name in BOUND_FIELDS
        if t.get(name) and binding.get(name) and t[name] != binding[name]
    ]
    wrong = [f"trace {t['id']} {name} {t[name]}, expected {binding[name]}" for t, name in mismatched]
    faults = {f"{name}={t[name]}" for t, name in mismatched}
    local = binding["local"] or {}
    if binding["read"] and not binding["remote"] and local.get("accepted"):
        wrong.append(
            f"session {binding['session_id']} accepted {local['accepted']} observations, "
            f"none under agent:{binding['agent']}"
        )
        faults.add("unattributed")
    if not wrong:
        return None
    return Finding(
        "telemetry misattributed",
        f"{binding['agent']}.{binding['started_at']}.{'+'.join(sorted(faults))}",
        f"telemetry of {binding['agent']} carries another binding",
        (*_who(binding), *wrong),
        "every trace tagged with the agent carries its life, seat, task, harness and resolved profile",
        len(wrong),
    )


def _start_unknown(binding):
    return Finding(
        "telemetry start unknown",
        binding["agent"],
        "working agent start is unknown",
        (*_who(binding), "no positive spawn or launch timestamp; launch grace cannot be established"),
        "an observed start is required to judge active telemetry",
        1,
    )


def _active_binding(binding, record, limits):
    if (binding.get("started_at") or 0) <= 0:
        return [_start_unknown(binding)]
    if record["now_ms"] - binding["started_at"] <= limits.active_grace_seconds * SECOND_MS:
        return []
    wrong = _misattributed(binding)
    found = [wrong] if wrong else []
    lag = unaccepted_seconds(binding, record["now_ms"])
    if lag > limits.active_stale_seconds:
        found.append(_stale(binding, record, limits, lag))
    elif binding["read"] and not binding["remote"] and not wrong:
        found.append(_never_exported(binding, record, limits))
    else:
        found += [f for f in (_unconfirmed(binding, record, limits),) if f]
    return found


def active(record: dict, limits: Limits) -> list[Finding]:
    return [f for binding in record.get("active", []) for f in _active_binding(binding, record, limits)]


def _caps(historical: dict) -> tuple:
    return (
        *(() if historical.get("listed", True) else ("trace listing stopped at its page cap",)),
        *(
            (f"{historical['unlisted']} traces past where the listing stopped not counted",)
            if historical.get("unlisted")
            else ()
        ),
        *((f"{historical['capped']} traces read up to the observation page cap",) if historical.get("capped") else ()),
    )


def _failure_detail(failures: list) -> tuple:
    dropped = len(failures) - FAILURE_DETAIL
    overflow = (f"{dropped} more failed reads past the bound of {FAILURE_DETAIL} per pass",) if dropped > 0 else ()
    return (*failures[:FAILURE_DETAIL], *overflow)


def reader(record: dict, limits: Limits) -> list[Finding]:
    state = record.get("reader")
    if not state:
        return []
    bindings, historical = state["active"], state["historical"]
    coverage = (
        f"active bindings read {bindings['read']} of {bindings['bindings']}",
        f"observations read for {historical['covered']} of {historical['traces']} traces",
    )
    found = []
    if state["failures"]:
        found.append(
            Finding(
                "trace reader unavailable",
                f"{record['slug']}.{state['down_since']}",
                f"{len(state['failures'])} Langfuse reads failed or ran out of time",
                (
                    *_failure_detail([*state["failures"], *historical.get("unreadable", [])]),
                    *coverage,
                    "unread bindings are not judged on Langfuse evidence",
                ),
                f"any failed or unfinished Langfuse read, {_cadence(limits)}",
                len(state["failures"]),
            )
        )
    if not historical["complete"] and not state["failures"]:
        missing = historical["traces"] - historical["covered"] - historical.get("capped", 0)
        found.append(
            Finding(
                "trace coverage partial",
                record["slug"],
                f"observations of {missing} traces not read yet",
                (
                    coverage[1],
                    *_caps(historical),
                    *_failure_detail(historical.get("unreadable", [])),
                    "tool error, task cost and turn gap findings cover only the read traces",
                    "each Doctor scan reads more within its read budget",
                ),
                "every trace's observations read",
                max(missing, 1),
            )
        )
    return found


def measures(record: dict) -> dict:
    now = record["now_ms"]
    reader_state = record.get("reader") or {}
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
        "active": [
            {
                "agent": b["agent"],
                "seat": b["seat"],
                "task": b["task"],
                "session": b["session_id"],
                "read": b["read"],
                "unaccepted_seconds": unaccepted_seconds(b, now),
                "langfuse_fresh_seconds": _ago(now, b["remote"]["fresh_ms"])
                if b["remote"] and b["remote"]["fresh_ms"]
                else None,
            }
            for b in record.get("active", [])
        ],
        "coverage": {key: reader_state[key] for key in ("active", "historical") if key in reader_state},
    }


def findings(record: dict, limits: Limits) -> list[Finding]:
    return [
        *tool_errors(record, limits),
        *task_cost(record, limits),
        *turn_gaps(record, limits),
        *wait_coverage(record, limits),
        *untraced(record, limits),
        *active(record, limits),
        *reader(record, limits),
    ]
