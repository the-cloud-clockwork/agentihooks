import re

from scripts.swarm.health.findings import Finding

TICK_MS = 60_000
FAILURE = re.compile(r"^(?:spawn failed for ([^\s,]+).*?|master spawn failed): (.+)$")


def failed(record: dict) -> list[Finding]:
    failures = {}
    prefix = f"{record['slug']}: "
    for action in record["actions"]:
        if not action.startswith(prefix):
            continue
        match = FAILURE.fullmatch(action[len(prefix) :])
        if match:
            failures.setdefault(match[1] or "master", []).append(match[2])
    return [
        Finding(
            "failed spawn",
            subject,
            f"{len(reasons)} failed spawns",
            tuple(reasons),
            "at least one failed spawn",
            len(reasons),
        )
        for subject, reasons in sorted(failures.items())
    ]


def fresh_restores(record: dict) -> list[Finding]:
    return [
        Finding(
            "fresh restore",
            outcome["name"],
            "restore fell back to a fresh session",
            (
                outcome["reason"],
                f"task {outcome['task']}",
                f"conversation {outcome.get('conversation_id', '')}",
                f"at {outcome['at']}",
            ),
            "restore outcome is fresh",
            1,
        )
        for outcome in record["restored"]
        if outcome["outcome"] == "fresh"
    ]


def silent_starts(agents: list[dict], hooked: dict, now_ms: int) -> list[Finding]:
    return [
        Finding(
            "no hook event",
            agent["name"],
            "spawned agent has run no tool since it started",
            (
                f"started at {agent['started_at']}",
                f"harness {agent.get('harness', '')}",
                f"pane {agent.get('pane_id', '')}",
            ),
            "no hook event one tick after spawn",
            (now_ms - agent["started_at"]) // TICK_MS,
        )
        for agent in agents
        if agent.get("state") != "finished"
        and agent.get("started_at")
        and agent["name"] not in hooked
        and now_ms - agent["started_at"] >= TICK_MS
    ]


def findings(record: dict) -> list[Finding]:
    return [*failed(record), *fresh_restores(record)]
