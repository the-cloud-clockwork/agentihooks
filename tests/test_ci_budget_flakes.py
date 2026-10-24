import io
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from scripts.ci_budget import defects, flakes
from scripts.swarm.ledger_client import LedgerGone
from scripts.swarm.notice_text import plain
from scripts.swarm.store import PREFIX, SwarmError
from scripts.swarm_ledger import ledger_comments

NOW_MS = 1_791_700_000_000
TEST = "tests/swarm_ledger/test_alerts_page.py::test_the_panel_fold_is_remembered_after_a_reload"
OTHER = "tests/Inbox_Cases/test_Notify.py::TestNotify::test_Notify_Within_Budget"
TEXT = (
    "The test the panel fold is remembered after a reload in the swarm ledger alerts page tests passed only"
    " on a retry in 2 dev runs within seven days."
)
LISTING = "repos/{owner}/{repo}/actions/artifacts?name=flake-budget&per_page=100"
JQ = '[.artifacts[] | select((.expired | not) and .workflow_run.head_branch == "dev")][0].id // empty'


def _zip(budget, name="flake-budget.json"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, json.dumps(budget))
    return buffer.getvalue()


def _budget(*tests):
    return {"flaky_tests": len(tests) + 1, "over_budget": [{"test": t, "runs": ["31", "32"]} for t in tests]}


def _gh(calls, budget=None, artifact="77", archive=None):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "/commits?" in command[2]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[2] == LISTING:
            return subprocess.CompletedProcess(command, 0, f"{artifact}\n", "")
        return subprocess.CompletedProcess(command, 0, archive if archive is not None else _zip(budget), b"")

    return run


@pytest.fixture
def swarm():
    import fakeredis

    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    ledger = SimpleNamespace(added=[])
    ledger.followup = lambda slug, text: ledger.added.append((slug, text))
    return store, SimpleNamespace(repo="/repo"), ledger


def test_the_newest_dev_budget_is_read_from_the_repository():
    calls = []
    assert flakes.read_budget("/repo", "dev", _gh(calls, _budget(TEST))) == _budget(TEST)
    assert calls == [
        (
            ["gh", "api", LISTING, "--jq", JQ],
            {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60},
        ),
        (
            ["gh", "api", "repos/{owner}/{repo}/actions/artifacts/77/zip"],
            {"cwd": "/repo", "capture_output": True, "check": True, "timeout": 60},
        ),
    ]
    flakes.read_budget("/repo", "diffcheck/x", _gh(calls, _budget()))
    assert calls[2][0][4] == JQ.replace('"dev"', '"diffcheck/x"')


def test_no_budget_on_the_branch_reads_nothing_more():
    calls = []
    assert flakes.read_budget("/repo", "dev", _gh(calls, artifact="")) is None
    assert len(calls) == 1


def test_a_test_flaky_in_two_runs_is_filed_once_in_plain_words(swarm):
    store, config, ledger = swarm
    calls = []
    actions = flakes.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls, _budget(TEST, OTHER)))
    assert ledger.added == [
        ("sw", TEXT),
        (
            "sw",
            "The test notify within budget in the inbox cases notify tests passed only on a retry in 2 dev runs"
            " within seven days.",
        ),
    ]
    assert all(ledger_comments.problems(text, "item") == [] and plain(text, "item") == text for _, text in ledger.added)
    assert actions == ["filed a ledger follow up for a test flaky in two dev runs within seven days"] * 2
    assert calls[0][0][4] == JQ and calls[0][1]["cwd"] == "/repo"
    raised = f"{PREFIX}:sw:ci-budget:flaky:{TEST}"
    assert flakes.RAISED_TTL_S == 604800
    assert 604800 - 60 < store.redis.ttl(raised) <= 604800

    later = NOW_MS + defects.REFRESH_MS
    assert flakes.refresh("sw", config, store, ledger, later, run=_gh(calls, _budget(TEST, OTHER))) == []
    assert len(ledger.added) == 2
    newer = "tests/test_page.py::test_reload"
    flakes.refresh("sw", config, store, ledger, later + defects.REFRESH_MS, run=_gh(calls, _budget(TEST, newer)))
    assert [text for _, text in ledger.added[2:]] == [
        "The test reload in the page tests passed only on a retry in 2 dev runs within seven days."
    ]


def test_the_budget_is_read_at_most_once_per_refresh_interval(swarm):
    store, config, ledger = swarm
    calls = []
    flakes.refresh("sw", config, store, ledger, defects.REFRESH_MS, run=_gh(calls, _budget()))
    assert len(calls) == 2
    flakes.refresh("sw", config, store, ledger, 2 * defects.REFRESH_MS - 1, run=_gh(calls, _budget()))
    assert len(calls) == 2
    flakes.refresh("sw", config, store, ledger, 2 * defects.REFRESH_MS, run=_gh(calls, _budget()))
    assert len(calls) == 4
    assert store.redis.get(defects.key("sw", "flakes")) == str(2 * defects.REFRESH_MS)


def test_a_branch_with_no_budget_files_nothing(swarm):
    store, config, ledger = swarm
    calls = []
    assert flakes.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls, artifact=""), branch="diffcheck/x") == []
    assert ledger.added == []
    assert calls[0][0][4] == JQ.replace('"dev"', '"diffcheck/x"')


def _github_down(command, **kwargs):
    raise subprocess.CalledProcessError(1, command, "", "HTTP 502")


@pytest.mark.parametrize(
    "broken",
    [_github_down, _gh([], archive=b"not a zip"), _gh([], archive=_zip(_budget(), name="other.json"))],
)
def test_an_unreadable_budget_files_nothing_and_retries_next_interval(swarm, capsys, broken):
    store, config, ledger = swarm
    assert flakes.refresh("sw", config, store, ledger, NOW_MS, run=broken) == []
    assert ledger.added == []
    assert capsys.readouterr().err.startswith("ci budget skipped reading the flake budget: ")
    flakes.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([], _budget(TEST)))
    assert ledger.added == [("sw", TEXT)]


def test_a_read_error_names_what_github_said(swarm, capsys):
    store, config, ledger = swarm
    flakes.refresh("sw", config, store, ledger, NOW_MS, run=_github_down)
    assert capsys.readouterr().err == "ci budget skipped reading the flake budget: HTTP 502\n"


def test_a_followup_the_ledger_refuses_is_not_sent_again(swarm):
    store, config, ledger = swarm
    refused = []

    def refuse(slug, text):
        refused.append(text)
        return False

    ledger.followup = refuse
    assert flakes.refresh("sw", config, store, ledger, NOW_MS, run=_gh([], _budget(TEST))) == []
    flakes.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([], _budget(TEST)))
    assert refused == [TEXT]


@pytest.mark.parametrize("error", [ConnectionRefusedError, LedgerGone, SwarmError])
def test_a_followup_that_fails_to_reach_the_ledger_is_retried_next_interval(swarm, capsys, error):
    store, config, ledger = swarm

    def unreachable(slug, text):
        raise error("ledger down")

    ledger.followup = unreachable
    assert flakes.refresh("sw", config, store, ledger, NOW_MS, run=_gh([], _budget(TEST, OTHER))) == []
    assert capsys.readouterr().err == "ci budget skipped one flaky test: ledger down\n" * 2
    ledger.followup = lambda slug, text: ledger.added.append((slug, text))
    flakes.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([], _budget(TEST)))
    assert ledger.added == [("sw", TEXT)]
