def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports) -> list[str]:
    named = [(fix, *branch_fix(fix)) for fix in fixes]
    # ruleid: panic-sweep-rereads-merged-fixes
    changes = [fix_change(fix) for fix, _, branch in named if not branch]
    return [change.url for change in changes]


def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports) -> list[str]:
    named = [(fix, *branch_fix(fix)) for fix in fixes]
    # ruleid: panic-sweep-rereads-merged-fixes
    changes = [stack.change(fix, f"cannot read pull request {fix}") for fix, _, branch in named if not branch]
    return [change.url for change in changes]


def cancel_runs(
    fixes: Iterable[str], repos: Iterable[str], ports: Ports, store: RedisStore | None = None
) -> list[str]:
    named = [(fix, *branch_fix(fix)) for fix in fixes]
    # ok: panic-sweep-rereads-merged-fixes
    changes = [fix_state(store, fix) for fix, _, branch in named if not branch]
    return [change.url for change in changes]


def fix_state(store: RedisStore | None, url: str) -> ChangeState:
    # ok: panic-sweep-rereads-merged-fixes
    return fix_change(url)
