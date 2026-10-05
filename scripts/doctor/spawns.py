import re

from scripts.swarm.health.findings import Finding

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


def findings(record: dict) -> list[Finding]:
    return [*failed(record), *share_drift(record), *overflow(record), *fresh_restores(record)]
