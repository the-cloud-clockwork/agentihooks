"""The Handoff v2 envelope: every fact of a transfer the runtime already holds, so the agent never types them.

A fact the runtime looked for and could not find is "unknown"; one it found absent is "none" or an empty list.
"""

import json
import subprocess
from datetime import datetime, timezone

from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm import effort_range, snapshot
from scripts.swarm.ledger_events import RED
from scripts.swarm.naming import plain
from scripts.swarm.store import MASTER

REASONS = ("recycle", "quota", "succession", "takeover", "reopen", "restore", "inbox", "exit", "operator")
UNKNOWN, NONE = "unknown", "none"
LAUNCH = (
    "profile",
    "harness",
    "model",
    "effort",
    "account",
    "model_source",
    "model_confidence",
    "profile_decision",
    "overlays",
)
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILED = (OSError, subprocess.SubprocessError)


def build(store, slug, agent, reason, rows, at, run=subprocess.run):
    """The envelope for agent handing off task work; rows are the ledger's tasks, None when the ledger was unreadable."""
    if reason not in REASONS:
        raise ValueError(f"handoff reason {reason!r} is not one of {', '.join(REASONS)}")
    row = next((r for r in rows or [] if r.get("id") == agent.task), None)
    worktree = _worktree(store, slug, agent, run)
    branch = _branch(worktree, run) if worktree else UNKNOWN
    return {
        "seat": agent.seat or UNKNOWN,
        "agent": agent.name,
        "reason": reason,
        "task": agent.task,
        "phase": (row or {}).get("phase") or UNKNOWN,
        "ledger": slug,
        "time": datetime.fromtimestamp(at / 1000, timezone.utc).isoformat(),
        "worktree": worktree or UNKNOWN,
        "branch": branch,
        **continuation(worktree, branch, run),
        "pull_request": _pull_request(row, run),
        "inbox": _open_items(store, agent),
        "claims": UNKNOWN if rows is None else [r["id"] for r in rows if store.claimant(slug, r["id"]) == agent.name],
        "conversation_id": agent.conversation_id or UNKNOWN,
        "launch": _launch(store, slug, agent),
    }


def _launch(store, slug, agent):
    launch = {key: getattr(agent, key) for key in LAUNCH}
    if agent.lane == MASTER and agent.harness in effort_range.EFFORTS:
        launch["effort"] = effort_range.clamp(agent.harness, agent.effort, effort_range.of(store.config(slug)))
    return launch


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


def continuation(worktree, branch, run):
    """Where a successor on the same task cuts its worktree: the predecessor's branch on the remote, else fresh and why."""
    remote = _remote_branch(worktree, branch, run) if branch != UNKNOWN else UNKNOWN
    head = _remote_head(worktree, remote, run) if remote != UNKNOWN else UNKNOWN
    if head not in (UNKNOWN, NONE):
        return {"remote_branch": remote, "remote_head": head, "continue_from": f"origin/{remote}", "fresh_reason": NONE}
    if remote == UNKNOWN:
        why = "the predecessor's branch is unknown"
    elif head == NONE:
        why = f"branch {remote} is not on the remote"
    else:
        why = f"the remote head of branch {remote} could not be read"
    return {"remote_branch": remote, "remote_head": head, "continue_from": "fresh", "fresh_reason": why}


def reclaim(repo: str, lives: list[str], recorded: str, run=subprocess.run) -> dict:
    """Where a claimant after retired lives cuts its worktree: the newest branch an earlier life pushed, else fresh and why.

    lives are the earlier agents on the task, newest first; recorded is the branch the ledger holds for it, if any.
    """
    try:
        trees = snapshot.worktrees(repo, lives, run)
    except FAILED:
        trees = {}
    branches = [_remote_branch(trees[name], plain(name), run) if trees.get(name) else plain(name) for name in lives]
    checked, unread = list(dict.fromkeys([*branches, *([recorded] if recorded else [])])), []
    for branch in checked:
        head = _remote_head(repo, branch, run)
        if head == UNKNOWN:
            unread.append(branch)
        elif head != NONE:
            return {
                "remote_branch": branch,
                "remote_head": head,
                "continue_from": f"origin/{branch}",
                "fresh_reason": NONE,
            }
    if unread:
        why = f"the remote heads of {', '.join(unread)} could not be read"
    else:
        names = ", ".join(checked[:-1]) + " and " + checked[-1] if len(checked) > 1 else checked[0]
        why = f"no earlier life pushed a branch: checked {names} on the remote"
    return {"remote_branch": NONE, "remote_head": NONE, "continue_from": "fresh", "fresh_reason": why}


def _remote_branch(worktree, branch, run):
    argv = ["git", "-C", worktree, "rev-parse", "--abbrev-ref", "@{upstream}"]
    try:
        upstream = run(argv, capture_output=True, text=True, timeout=10).stdout.strip()
    except FAILED:
        return branch
    return upstream.removeprefix("origin/") if upstream.startswith("origin/") else branch


def _remote_head(worktree, remote, run):
    argv = ["git", "-C", worktree, "ls-remote", "--exit-code", "origin", f"refs/heads/{remote}"]
    try:
        done = run(argv, capture_output=True, text=True, timeout=20)
    except FAILED:
        return UNKNOWN
    if done.returncode == 2:
        return NONE
    return done.stdout.split()[0] if done.returncode == 0 else UNKNOWN


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
