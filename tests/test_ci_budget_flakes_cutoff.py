import subprocess
from types import SimpleNamespace

import pytest

from scripts.ci_budget import defects, flakes
from tests.test_ci_budget_flakes import NOW_MS, TEST, _budget, _gh


@pytest.mark.parametrize(
    ("last_retry", "merged", "remaining"),
    [
        ("2026-10-22T00:00:00Z", "2026-10-23T12:00:00Z", 0),
        ("2026-10-24T00:00:00Z", "2026-10-23T12:00:00Z", 1),
        ("2026-10-23T12:00:00Z", "2026-10-23T12:00:00Z", 1),
        ("2026-10-24T00:00:00Z", "", 2),
    ],
    ids=["before-fix", "after-fix", "at-merge", "no-merge"],
)
def test_followups_only_count_retry_passes_after_the_latest_test_fix(last_retry, merged, remaining):
    import fakeredis

    added = []
    ledger = SimpleNamespace(followup=lambda slug, text: added.append(text))
    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    config = SimpleNamespace(repo="/repo")
    artifact = _gh([], _budget(TEST))
    responses = {
        "repos/{owner}/{repo}/commits?sha=dev&path=tests%2Fswarm_ledger%2Ftest_alerts_page.py&per_page=1": (
            ".[0].sha // empty",
            "fix-sha\n",
        ),
        "repos/{owner}/{repo}/commits/fix-sha/pulls": (
            '[.[] | select(.base.ref == "dev" and .merged_at != null) | .merged_at] | max // empty',
            merged + "\n",
        ),
        "repos/{owner}/{repo}/actions/runs/31": (".created_at", "2026-10-22T00:00:00Z\n"),
        "repos/{owner}/{repo}/actions/runs/32": (".created_at", last_retry + "\n"),
    }
    calls = []

    def run(command, **kwargs):
        if "/actions/artifacts" in command[2]:
            return artifact(command, **kwargs)
        calls.append(command)
        query, output = responses[command[2]]
        assert command == ["gh", "api", command[2], "--jq", query]
        assert kwargs == {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60}
        return subprocess.CompletedProcess(command, 0, output, "")

    actions = flakes.refresh("sw", config, store, ledger, NOW_MS, run=run)

    if remaining:
        assert len(added) == 1
        assert f"retry in {remaining} dev runs" in added[0]
    else:
        assert added == []
        assert not store.redis.exists(defects.key("sw", "flaky", TEST))
    before = 2 - remaining
    assert actions == ([f"{before} retry passes were before the newest merged test change"] if before else []) + (
        ["filed a ledger follow up for a test flaky in two dev runs within seven days"] if remaining else []
    )
    assert len(calls) == (4 if merged else 2)
