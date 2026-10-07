"""Park a stacked task on its pushed branch until its open dependencies merge."""

import json
import subprocess

from scripts.handoff import check as handoff_check
from scripts.handoff.resolve import Resolver
from scripts.swarm.store import SwarmError


def shell(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SwarmError(f"{' '.join(argv[:2])} could not run: {exc}") from exc


def _out(argv, refused):
    done = shell(argv)
    if done.returncode != 0:
        raise SwarmError(f"{refused}: {done.stderr.strip()}")
    return done.stdout.strip()


def _pushed(branch):
    if not branch:
        raise SwarmError("the task records no branch; push it and run swarm branch before parking")
    remote = shell(["git", "ls-remote", "--heads", "origin", branch]).stdout.split()
    if not remote:
        raise SwarmError(f"branch {branch} is not on origin; push it first with git push -u origin {branch}")
    if remote[0] != _out(["git", "rev-parse", "HEAD"], "cannot read the worktree head"):
        raise SwarmError(f"the worktree holds commits origin lacks; push {branch} first")


def _issue_ready(row):
    if row.get("issue_url"):
        return
    answer = _out(["gh", "repo", "view", "--json", "hasIssuesEnabled"], "cannot tell whether the repo has issues")
    try:
        has_issues = json.loads(answer)["hasIssuesEnabled"]
    except (ValueError, KeyError) as exc:
        raise SwarmError(f"cannot tell whether the repo has issues: {exc}") from exc
    if has_issues:
        raise SwarmError("the repo has issues; open one and run swarm issue before parking")


def _open_dependencies(row, rows):
    blockers = [rows.get(d, {"id": d}) for d in row.get("depends_on") or []]
    open_ = [b for b in blockers if not b.get("done") and b.get("state") != "done"]
    if not open_:
        raise SwarmError("the task has no open dependency to park on; finish it instead")
    if not any(b.get("branch") for b in open_):
        raise SwarmError("no open dependency records a branch, so there is nothing to stack on")
    return open_


def _stacked_base(open_):
    bases = []
    for branch in (b["branch"] for b in open_ if b.get("branch")):
        _out(["git", "fetch", "origin", branch], f"cannot fetch {branch}")
        bases.append(_out(["git", "merge-base", "HEAD", f"origin/{branch}"], f"{branch} shares no history"))
    return max(bases, key=lambda sha: int(_out(["git", "rev-list", "--count", sha], f"cannot count {sha}")))


def _issue_body(row, open_, base):
    blockers = ", ".join(f"{b['id']} (`{b['branch']}`)" if b.get("branch") else b["id"] for b in open_)
    return (
        f"Parked on branch `{row['branch']}` until {blockers} merges. Stacked base `{base}`. "
        "The next engineer restacks it onto dev and finishes it."
    )


def _ledger_note(open_):
    names = " and ".join(b.get("title") or "its dependency" for b in open_)
    return f"Parked on its branch until {names} is done. The next engineer restacks it onto dev and finishes it."


def park(store, slug: str, agent, text: str, ledger) -> dict:
    state = ledger.state(slug)
    rows = {t["id"]: t for t in state["tasks"]}
    row = rows.get(agent.task) or {}
    _pushed(row.get("branch"))
    _issue_ready(row)
    open_ = _open_dependencies(row, rows)
    found = handoff_check.problems(text, Resolver(slug, store.redis, ledger.state))
    if found:
        raise SwarmError(handoff_check.refusal(found))
    base = _stacked_base(open_)
    if row.get("issue_url"):
        body = _issue_body(row, open_, base)
        _out(["gh", "issue", "comment", row["issue_url"], "--body", body], "could not comment on the issue")
    fields = {"parked_on": [b["id"] for b in open_], "stacked_base": base}
    ledger.update_task(slug, agent.task, fields, by=agent.name)
    ledger.comment(slug, agent.task, _ledger_note(open_), by=agent.name)
    return fields


def _restack_branch() -> None:
    branch = _out(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], "restack needs a task worktree branch")
    if branch in ("dev", "main", "master", "v1"):
        raise SwarmError("restack needs a task worktree branch")
    if _out(["git", "status", "--porcelain"], "cannot inspect the worktree"):
        raise SwarmError("restack needs a clean worktree")


def _rebase(base: str) -> None:
    base = _out(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], "cannot resolve the stacked base"
    )
    if shell(["git", "merge-base", "--is-ancestor", "origin/dev", "HEAD"]).returncode == 0:
        return
    if shell(["git", "merge-base", "--is-ancestor", base, "HEAD"]).returncode != 0:
        raise SwarmError("the stacked base is not in this branch")
    done = shell(["git", "rebase", "--onto", "origin/dev", base])
    if done.returncode != 0:
        files = _out(["git", "diff", "--name-only", "--diff-filter=U"], "cannot list conflicted files")
        raise SwarmError(
            f"restack failed: {done.stderr.strip()}\nConflicted files:\n{files}\n"
            "Resolve the files, run git rebase --continue, then run swarm restack again."
        )


def restack(slug: str, agent, ledger) -> dict:
    rows = {t["id"]: t for t in ledger.state(slug)["tasks"]}
    row = rows.get(agent.task) or {}
    if not row.get("parked_on") or not row.get("stacked_base"):
        raise SwarmError("this task has no parked work with a stacked base")
    if any(rows.get(dep, {}).get("state") != "done" for dep in row["parked_on"]):
        raise SwarmError("this task still has an unfinished parked dependency")
    _restack_branch()
    _out(["git", "fetch", "origin"], "cannot fetch origin")
    _rebase(row["stacked_base"])
    fields = {"parked_on": []}
    ledger.update_task(slug, agent.task, fields, by=agent.name)
    return fields
