def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports) -> list[str]:
    named = [(fix, *branch_fix(fix)) for fix in fixes]
    changes = [fix_change(fix) for fix, _, branch in named if not branch]
    # ruleid: panic-sweep-keeps-fix-heads-everywhere
    branches = {change.head_branch for change in changes}
    cancelled: list[str] = []
    for repo in repos:
        provider = ports(repo)
        rows = provider.latest_runs(deadline=Deadline.after(TOOL_SECONDS))
        here = repo_key(f"{GITHUB}{repo}")
        kept = branches | {branch for _, key, branch in named if key == here}
        for row in rows:
            if _spared(row, kept, set()):
                continue
            cancelled.append(f"{repo} run {row.id} on {row.branch}")
    return cancelled


def cancel_runs_in_their_repository(fixes: Iterable[str], repos: Iterable[str], ports: Ports) -> list[str]:
    named = [(fix, *branch_fix(fix)) for fix in fixes]
    changes = [fix_change(fix) for fix, _, branch in named if not branch]
    cancelled: list[str] = []
    for repo in repos:
        provider = ports(repo)
        rows = provider.latest_runs(deadline=Deadline.after(TOOL_SECONDS))
        here = repo_key(f"{GITHUB}{repo}")
        # ok: panic-sweep-keeps-fix-heads-everywhere
        kept = {change.head_branch for change in changes if change.reference.repository == repo}
        kept |= {branch for _, key, branch in named if key == here}
        for row in rows:
            if _spared(row, kept, set()):
                continue
            cancelled.append(f"{repo} run {row.id} on {row.branch}")
    return cancelled
