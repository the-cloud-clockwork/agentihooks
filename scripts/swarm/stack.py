"""Park a stacked task on its pushed branch until its open dependencies merge."""

import json
import subprocess
from pathlib import Path

from scripts.handoff import check as handoff_check
from scripts.handoff.resolve import Resolver
from scripts.swarm.store import SwarmError

WT_SCRIPT = Path(__file__).resolve().parents[2] / "profiles" / "package" / "skills" / "worktree" / "scripts" / "wt.sh"


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
    if _out(["git", "status", "--porcelain"], "cannot inspect the worktree"):
        raise SwarmError(f"the worktree holds uncommitted changes; commit them and push {branch} first")


def _worktree() -> str:
    top = _out(["git", "rev-parse", "--show-toplevel"], "cannot locate the worktree")
    common = _out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], "cannot locate the repo")
    root = _out(["bash", str(WT_SCRIPT), "root"], "cannot locate the worktree root")
    if Path(top).resolve().parent != Path(root).resolve() / Path(common).parent.name:
        raise SwarmError(f"park removes only a worktree wt.sh new made under {root}; {top} is not one")
    return top


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


def repo_key(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep and ":" not in url.partition("/")[0]:
        return url.rstrip("/").removesuffix(".git")
    host, slash, path = (rest if sep else url).lower().partition("/")
    rest = host.rpartition("@")[2].replace(":", "/", 1) + slash + path
    return rest.rstrip("/").removesuffix(".git")


def public_url(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    host, slash, path = rest.partition("/")
    user, at, place = host.rpartition("@")
    kept = f"{user.partition(':')[0]}@" if at and not scheme.lower().startswith("http") else ""
    return f"{scheme}://{kept}{place}{slash}{path}"


def _foreign(open_):
    marked = [b for b in open_ if b.get("branch") and b.get("branch_repo")]
    home = repo_key(_out(["git", "remote", "get-url", "origin"], "cannot read the origin url")) if marked else ""
    return [b for b in marked if repo_key(b["branch_repo"]) != home]


def _base_of(dep, foreign):
    branch = dep["branch"]
    if dep not in foreign:
        _out(["git", "fetch", "origin", branch], f"cannot fetch {branch}")
        return _out(["git", "merge-base", "HEAD", f"origin/{branch}"], f"{branch} shares no history")
    _out(["git", "fetch", "--", dep["branch_repo"], branch], f"cannot fetch {branch} from {dep['branch_repo']}")
    _out(["git", "fetch", "origin", "dev"], "cannot fetch dev")
    return _out(["git", "merge-base", "HEAD", "origin/dev"], "the task shares no history with dev")


def _stacked_base(open_, foreign):
    bases = [_base_of(b, foreign) for b in open_ if b.get("branch")]
    return max(bases, key=lambda sha: int(_out(["git", "rev-list", "--count", sha], f"cannot count {sha}")))


def _blocker(dep, foreign):
    if not dep.get("branch"):
        return dep["id"]
    where = f" in {dep['branch_repo']}" if dep in foreign else ""
    return f"{dep['id']} (`{dep['branch']}`{where})"


def _issue_body(row, open_, base, foreign):
    blockers = ", ".join(_blocker(b, foreign) for b in open_)
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
    top = _worktree()
    _issue_ready(row)
    open_ = _open_dependencies(row, rows)
    found = handoff_check.problems(text, Resolver(slug, store.redis, ledger.state))
    if found:
        raise SwarmError(handoff_check.refusal(found))
    foreign = _foreign(open_)
    base = _stacked_base(open_, foreign)
    if row.get("issue_url"):
        body = _issue_body(row, open_, base, foreign)
        _out(["gh", "issue", "comment", row["issue_url"], "--body", body], "could not comment on the issue")
    repos = {}
    for dep in foreign:
        repos.setdefault(repo_key(dep["branch_repo"]), dep["branch_repo"])
    fields = {"parked_on": [b["id"] for b in open_], "stacked_base": base, "parked_repos": list(repos.values())}
    ledger.update_task(slug, agent.task, fields, by=agent.name)
    ledger.comment(slug, agent.task, _ledger_note(open_), by=agent.name)
    return fields, top


def remove_worktree(top: str) -> str:
    refused = f"the task is parked and its seat handed off, but wt.sh done could not remove {top}"
    _out(["bash", str(WT_SCRIPT), "done", Path(top).name, "--repo", top], refused)
    return top


def _restack_branch() -> str:
    branch = _out(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], "restack needs a task worktree branch")
    if branch in ("dev", "main", "master", "v1"):
        raise SwarmError("restack needs a task worktree branch")
    if _out(["git", "status", "--porcelain"], "cannot inspect the worktree"):
        raise SwarmError("restack needs a clean worktree")
    return branch


def _restack_base(row, context):
    base = _out(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{row['stacked_base']}^{{commit}}"],
        "cannot resolve the stacked base",
    )
    path = Path(_out(["git", "rev-parse", "--git-path", "agentihooks-restack.json"], "cannot locate restack state"))
    if (
        shell(["git", "merge-base", "--is-ancestor", base, "HEAD"]).returncode == 0
        and row.get("branch")
        and shell(["git", "merge-base", "--is-ancestor", f"origin/{row['branch']}", "HEAD"]).returncode == 0
    ):
        onto = _out(["git", "rev-parse", "origin/dev"], "cannot resolve origin/dev")
        path.write_text(json.dumps({"context": context, "onto": onto}))
        return base, path
    if path.exists():
        saved = json.loads(path.read_text())
        if (
            saved["context"] == context
            and shell(["git", "merge-base", "--is-ancestor", saved["onto"], "HEAD"]).returncode == 0
        ):
            return saved["onto"], path
    raise SwarmError("the stacked base is not in this branch; use a worktree cut from the parked branch")


def _rebase(base: str) -> None:
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
    branch = _restack_branch()
    _out(["git", "fetch", "origin"], "cannot fetch origin")
    base, path = _restack_base(row, [slug, agent.task, branch, row["stacked_base"]])
    _rebase(base)
    fields = {"parked_on": []}
    ledger.update_task(slug, agent.task, fields, by=agent.name)
    path.unlink()
    return fields
