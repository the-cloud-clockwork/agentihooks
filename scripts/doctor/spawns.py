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


def share_drift(record: dict) -> list[Finding]:
    counts, target = record["spawns"], record["target"]
    total, codex = sum(counts.values()), counts.get("codex", 0)
    if not total or abs(codex * 100 - total * target) <= 100:
        return []
    actual = codex * 100 / total
    return [
        Finding(
            "codex share drift",
            record["slug"],
            f"Codex share {actual:.1f}% against target {target}%",
            (f"codex {codex}/{total} spawns, {actual:.1f}%, target {target}%",),
            "more than one spawn from target",
            round(abs(actual - target)),
        )
    ]


def overflow(record: dict) -> list[Finding]:
    return [
        Finding(
            "account overflow",
            agent["name"],
            "session placed on an account at its cap",
            (f"account {agent['account']}", f"harness {agent['harness']}", "placement overflow"),
            "launcher reported overflow",
            1,
        )
        for agent in record["agents"]
        if agent.get("placement") == "overflow"
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
    return [*failed(record), *share_drift(record), *overflow(record), *fresh_restores(record)]
