"""Park a stacked task on its pushed branch until its open dependencies merge."""

import json
import subprocess
from pathlib import Path

from scripts.handoff import check as handoff_check
from scripts.handoff.resolve import Resolver
from scripts.swarm import apply_wait
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_comments, ledger_kinds

READ_ONLY_NOTE = "Released its seat while it waits on {names}. The next engineer continues it from the recorded window."
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
    listed = shell(["git", "ls-remote", "--heads", "origin", branch]).stdout.splitlines()
    remote = [line.split()[0] for line in listed if line.endswith(f"\trefs/heads/{branch}")]
    if not remote:
        raise SwarmError(f"branch {branch} is not on origin; push it first with git push -u origin {branch}")
    if remote[0] != _out(["git", "rev-parse", "HEAD"], "cannot read the worktree head"):
        raise SwarmError(f"the worktree holds commits origin lacks; push {branch} first")
    if _out(["git", "status", "--porcelain"], "cannot inspect the worktree"):
        raise SwarmError(f"the worktree holds uncommitted changes; commit them and push {branch} first")


def pushed(root: Path, recorded: str = "") -> bool:
    git = ["git", "-C", str(root)]
    local = _out([*git, "branch", "--show-current"], "cannot read the worktree branch")
    upstream = _out(
        [*git, "for-each-ref", "--format=%(upstream:remotename) %(upstream:remoteref)", f"refs/heads/{local}"],
        "cannot read the worktree upstream",
    )
    remote_name, _, remote_ref = upstream.partition(" ")
    branch = remote_ref.removeprefix("refs/heads/") or recorded or local
    remote_name = remote_name or "origin"
    listed = shell([*git, "ls-remote", "--heads", remote_name, branch]).stdout.splitlines()
    remote = [line.split()[0] for line in listed if line.endswith(f"\trefs/heads/{branch}")]
    if remote and shell([*git, "merge-base", "--is-ancestor", "HEAD", remote[0]]).returncode == 0:
        return True
    if remote or _out([*git, "rev-list", "--count", "origin/dev..HEAD"], "cannot count the worktree commits") != "0":
        target = branch if branch == local else f"HEAD:{branch}"
        raise SwarmError(
            f"the worktree {root} holds commits origin lacks; "
            f"push them with git -C {root} push -u {remote_name} {target} before blocking"
        )
    return False


def worktree() -> str:
    top = _out(["git", "rev-parse", "--show-toplevel"], "cannot locate the worktree")
    common = _out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], "cannot locate the repo")
    root = _out(["bash", str(WT_SCRIPT), "root"], "cannot locate the worktree root")
    if Path(top).resolve().parent != Path(root).resolve() / Path(common).parent.name:
        raise SwarmError(f"park and ready remove only a worktree wt.sh new made under {root}; {top} is not one")
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
    if not any(b.get("branch") for b in open_) and not all(ledger_kinds.read_only(b) for b in open_):
        raise SwarmError("no open dependency records a branch, so there is nothing to stack on")
    return open_


def repo_key(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep and ":" not in url.partition("/")[0]:
        return url.rstrip("/").removesuffix(".git")
    host, slash, path = (rest if sep else url).partition("/")
    rest = host.rpartition("@")[2].replace(":", "/", 1) + slash + path
    return rest.rstrip("/").lower().removesuffix(".git")


def origin_key(repo: str) -> str:
    try:
        return repo_key(shell(["git", "-C", repo, "remote", "get-url", "origin"]).stdout.strip())
    except SwarmError:
        return ""


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
    if not marked:
        return []
    home = repo_key(_out(["git", "remote", "get-url", "origin"], "cannot read the origin url"))
    return [b for b in marked if repo_key(b["branch_repo"]) != home]


def _merged_into_dev(dep):
    if not dep.get("pr_url"):
        return False
    answer = _out(
        ["gh", "pr", "view", dep["pr_url"], "--json", "state,baseRefName"],
        "cannot read the dependency pull request",
    )
    try:
        pull = json.loads(answer)
        return pull["state"] == "MERGED" and pull["baseRefName"] == "dev"
    except (ValueError, KeyError) as exc:
        raise SwarmError(f"cannot read the dependency pull request: {exc}") from exc


def _fetch_foreign(dep, branch):
    wanted = repo_key(dep["branch_repo"])
    checkouts = [path for path in dep.get("repository_branches") or {} if origin_key(path) == wanted]
    if checkouts:
        _out(["git", "-C", checkouts[0], "fetch", "origin", branch], f"cannot fetch {branch} in {checkouts[0]}")
    else:
        _out(["git", "fetch", "--", dep["branch_repo"], branch], f"cannot fetch {branch} from {dep['branch_repo']}")


def _base_of(dep, foreign):
    branch = dep["branch"]
    merged = _merged_into_dev(dep)
    if dep not in foreign and not merged:
        _out(["git", "fetch", "origin", branch], f"cannot fetch {branch}")
        return _out(["git", "merge-base", "HEAD", f"origin/{branch}"], f"{branch} shares no history")
    if not merged:
        _fetch_foreign(dep, branch)
    return _dev_base()


def _dev_base():
    _out(["git", "fetch", "origin", "dev"], "cannot fetch dev")
    return _out(["git", "merge-base", "HEAD", "origin/dev"], "the task shares no history with dev")


def _stacked_base(open_, foreign):
    bases = [_base_of(b, foreign) for b in open_ if b.get("branch")] or [_dev_base()]
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
    note = f"Parked on its branch until {names} is done. The next engineer restacks it onto dev and finishes it."
    if not ledger_comments.problems(note, "comment"):
        return note
    return "Parked on its branch until every open dependency is done. The next engineer restacks it onto dev and finishes it."


def park(store, slug: str, agent, text: str, ledger) -> dict:
    state = ledger.state(slug)
    rows = {t["id"]: t for t in state["tasks"]}
    row = rows.get(agent.task) or {}
    if ledger_kinds.read_only(row):
        return park_read_only(store, slug, agent, text, ledger, rows), ""
    _pushed(row.get("branch"))
    top = worktree()
    _issue_ready(row)
    open_ = _open_dependencies(row, rows)
    found = handoff_check.problems(text, Resolver(slug, store.redis, ledger.state))
    if found:
        raise SwarmError(handoff_check.refusal(found))
    foreign = _foreign(open_)
    base = _stacked_base(open_, foreign)
    ledger.comment(slug, agent.task, _ledger_note(open_), by=agent.name)
    if row.get("issue_url"):
        body = _issue_body(row, open_, base, foreign)
        _out(["gh", "issue", "comment", row["issue_url"], "--body", body], "could not comment on the issue")
    repos = {}
    for dep in foreign:
        repos.setdefault(repo_key(dep["branch_repo"]), dep["branch_repo"])
    fields = {"parked_on": [b["id"] for b in open_], "stacked_base": base, "parked_repos": list(repos.values())}
    ledger.update_task(slug, agent.task, fields, by=agent.name)
    return fields, top


def _read_only_note(open_):
    note = READ_ONLY_NOTE.format(names=" and ".join(b.get("title") or "its dependency" for b in open_))
    return note if not ledger_comments.problems(note, "comment") else READ_ONLY_NOTE.format(names="its dependencies")


def park_read_only(store, slug: str, agent, text: str, ledger, rows: dict) -> dict:
    row = rows[agent.task]
    held = apply_wait.waiting(store.config(slug), row, rows)
    deps = row.get("depends_on") or []
    open_ = [rows.get(d, {"id": d}) for d in deps if d in held or rows.get(d, {}).get("state") != "done"]
    if not open_:
        raise SwarmError("the task waits on no open dependency; finish it, or hand it off for its window")
    found = handoff_check.problems(text, Resolver(slug, store.redis, ledger.state))
    if found:
        raise SwarmError(handoff_check.refusal(found))
    ledger.comment(slug, agent.task, _read_only_note(open_), by=agent.name)
    fields = {"parked_on": [b["id"] for b in open_]}
    ledger.update_task(slug, agent.task, fields, by=agent.name)
    return fields


def remove_worktree(top: str) -> str:
    refused = f"the task is parked and its seat handed off, but wt.sh done could not remove {top}"
    _out(["bash", str(WT_SCRIPT), "done", Path(top).name, "--repo", top, "--pushed"], refused)
    return top


def _restack_branch() -> str:
    branch = _out(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], "restack needs a task worktree branch")
    if branch in ("dev", "main", "master", "v1"):
        raise SwarmError("restack needs a task worktree branch")
    if _out(["git", "status", "--porcelain"], "cannot inspect the worktree"):
        raise SwarmError("restack needs a clean worktree")
    return branch


def _restack_base(row, context):
    ref = f"refs/remotes/origin/{row.get('branch', '')}"
    if shell(["git", "show-ref", "--verify", "--quiet", ref]).returncode != 0:
        ref = f"refs/heads/{row.get('branch', '')}"
    base = _out(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{row['stacked_base']}^{{commit}}"],
        "cannot resolve the stacked base",
    )
    path = Path(_out(["git", "rev-parse", "--git-path", "agentihooks-restack.json"], "cannot locate restack state"))
    if (
        shell(["git", "merge-base", "--is-ancestor", base, "HEAD"]).returncode == 0
        and row.get("branch")
        and shell(["git", "merge-base", "--is-ancestor", ref, "HEAD"]).returncode == 0
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
    shell(["git", "branch", "--unset-upstream"])
    from . import publish

    root = Path(_out(["git", "rev-parse", "--show-toplevel"], "cannot locate the worktree"))
    fields = {"parked_on": []}
    update = {**fields, **publish.branch_record(publish.checkout_fields(root), publish.repository_root(root), row)}
    ledger.update_task(slug, agent.task, update, by=agent.name)
    path.unlink()
    return fields
