"""The Handoff v2 envelope: every fact of a transfer the runtime already holds, so the agent never types them.

A fact the runtime looked for and could not find is "unknown"; one it found absent is "none" or an empty list.
"""

import json
import subprocess
from datetime import datetime, timezone

from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import snapshot
from scripts.swarm.ledger_events import RED
from scripts.swarm.store import MASTER

REASONS = ("recycle", "quota", "succession", "takeover", "reopen", "restore", "inbox", "exit", "operator")
UNKNOWN, NONE = "unknown", "none"
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILED = (OSError, subprocess.SubprocessError)


def build(store, slug, agent, reason, rows, at, run=subprocess.run):
    """The envelope for agent handing off task work; rows are the ledger's tasks, None when the ledger was unreadable."""
    if reason not in REASONS:
        raise ValueError(f"handoff reason {reason!r} is not one of {', '.join(REASONS)}")
    row = next((r for r in rows or [] if r.get("id") == agent.task), None)
    worktree = _worktree(store, slug, agent, run)
    return {
        "seat": agent.seat or UNKNOWN,
        "agent": agent.name,
        "reason": reason,
        "task": agent.task,
        "phase": (row or {}).get("phase") or UNKNOWN,
        "ledger": slug,
        "time": datetime.fromtimestamp(at / 1000, timezone.utc).isoformat(),
        "worktree": worktree or UNKNOWN,
        "branch": _branch(worktree, run) if worktree else UNKNOWN,
        "pull_request": _pull_request(row, run),
        "inbox": _open_items(store, agent),
        "claims": UNKNOWN if rows is None else [r["id"] for r in rows if store.claimant(slug, r["id"]) == agent.name],
        "conversation_id": agent.conversation_id or UNKNOWN,
        "launch": {
            key: getattr(agent, key)
            for key in (
                "profile",
                "harness",
                "model",
                "effort",
                "account",
                "model_source",
                "model_confidence",
                "profile_decision",
            )
        },
    }


def _worktree(store, slug, agent, run):
    repo = store.config(slug).repo
    if agent.lane == MASTER:
        return repo
    try:
        return snapshot.worktrees(repo, [agent.name], run).get(agent.name, "")
    except FAILED:
        return ""


def _branch(worktree, run):
    try:
        done = run(["git", "-C", worktree, "branch", "--show-current"], capture_output=True, text=True, timeout=10)
    except FAILED:
        return UNKNOWN
    return (done.returncode == 0 and done.stdout.strip()) or UNKNOWN


def _pull_request(row, run):
    if row is None:
        return UNKNOWN
    url = row.get("pr_url")
    if not url:
        return NONE
    try:
        done = run(
            ["gh", "pr", "view", url, "--json", "state,statusCheckRollup"], capture_output=True, text=True, timeout=20
        )
        raw = json.loads(done.stdout) if done.returncode == 0 else None
    except (*FAILED, ValueError):
        raw = None
    if raw is None:
        return {"url": url, "state": UNKNOWN, "checks": UNKNOWN}
    return {"url": url, "state": raw.get("state") or UNKNOWN, "checks": _checks(raw.get("statusCheckRollup") or [])}


def _checks(rollup):
    counts = {"pass": 0, "fail": 0, "pending": 0}
    for check in rollup:
        outcome = check.get("conclusion") or check.get("state")
        counts["fail" if outcome in RED else "pass" if outcome in PASSED else "pending"] += 1
    return counts


def _open_items(store, agent):
    items = InboxStore(store.redis).mailbox(agent.name)
    return [{"id": i.id, "from": i.sender, "state": i.state} for i in items if i.state not in CLOSED]
