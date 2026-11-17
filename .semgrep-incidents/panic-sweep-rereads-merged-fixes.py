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


def fix_heads(fixes: Iterable[str]) -> set[str]:
    # ruleid: panic-sweep-rereads-merged-fixes
    return {fix_branch(fix) for fix in fixes}


def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports, store: RedisStore | None = None) -> list[str]:
    named = [(fix, *branch_fix(fix)) for fix in fixes]
    # ok: panic-sweep-rereads-merged-fixes
    changes = [fix_state(store, fix) for fix, _, branch in named if not branch]
    return [change.url for change in changes]


def fix_change(url: str) -> ChangeState:
    # ok: panic-sweep-rereads-merged-fixes
    return stack.change(url, f"cannot read pull request {url}")


def fix_state(store: RedisStore | None, url: str) -> ChangeState:
    if store is None:
        # ok: panic-sweep-rereads-merged-fixes
        return fix_change(url)
    # ok: panic-sweep-rereads-merged-fixes
    return fix_change(url)


def fix_branch(url: str) -> str:
    # ok: panic-sweep-rereads-merged-fixes
    return fix_change(url).head_branch


def check_fix(fix: str) -> None:
    # ok: panic-sweep-rereads-merged-fixes
    named = fix_branch(fix)
    if named in PROTECTED:
        raise SwarmError(f"{fix} names {named}")
