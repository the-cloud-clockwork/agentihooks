import subprocess
from types import SimpleNamespace

import pytest

from scripts.ci_budget import flakes
from tests.test_ci_budget_flakes import NOW_MS, TEST, _budget, _gh

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.mark.parametrize("newer", [False, True], ids=["before-fix", "after-fix"])
def test_followups_only_count_retry_passes_after_the_latest_test_fix(monkeypatch, newer):
    import fakeredis

    added = []
    ledger = SimpleNamespace(followup=lambda slug, text: added.append(text))
    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    config = SimpleNamespace(repo="/repo")
    artifact = _gh([], _budget(TEST))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        endpoint = command[2]
        if "/commits?" in endpoint:
            return subprocess.CompletedProcess(command, 0, "fix-sha\n", "")
        if "/commits/fix-sha/pulls" in endpoint:
            return subprocess.CompletedProcess(command, 0, "2026-10-23T12:00:00Z\n", "")
        if "/actions/runs/" in endpoint:
            at = "2026-10-24T00:00:00Z" if newer and endpoint.endswith("/32") else "2026-10-22T00:00:00Z"
            return subprocess.CompletedProcess(command, 0, at + "\n", "")
        return artifact(command, **kwargs)

    actions = flakes.refresh("sw", config, store, ledger, NOW_MS, run=run)

    if newer:
        assert len(added) == 1
        assert "retry in 1 dev runs" in added[0]
    else:
        assert added == []
    assert any("before the newest merged test change" in action for action in actions)
    assert any(
        "sha=dev" in command[2] and "path=tests%2Fswarm_ledger%2Ftest_alerts_page.py" in command[2] for command in calls
    )
    assert any(command[2].endswith("/commits/fix-sha/pulls") and "dev" in command[-1] for command in calls)
