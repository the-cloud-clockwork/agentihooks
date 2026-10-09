import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.ci_budget import defects
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm_ledger import ledger_comments

pytestmark = pytest.mark.xdist_group("fakeredis")

CI_SPEED_READ = True
NOW_MS = 1_791_540_000_000
URL = "https://github.com/o/r/actions/runs/{}/job/9"


def _run(run_id, started, updated, conclusion="success", branch="eng-1"):
    return {
        "id": run_id,
        "event": "pull_request",
        "status": "completed",
        "conclusion": conclusion,
        "head_branch": branch,
        "run_started_at": f"2026-10-09T07:{started}Z",
        "updated_at": f"2026-10-09T07:{updated}Z",
    }


def _jobs(run_id, gate_end):
    return [
        {
            "name": "mutation",
            "created_at": "2026-10-09T07:00:05Z",
            "completed_at": "2026-10-09T07:14:09Z",
            "conclusion": "success",
            "html_url": URL.format(run_id),
        },
        {
            "name": "Gate — Required",
            "created_at": "2026-10-09T07:14:10Z",
            "completed_at": f"2026-10-09T07:{gate_end}Z",
            "conclusion": "success",
            "html_url": URL.format(run_id),
        },
    ]


RUNS = [
    _run(1, "00:00", "16:08"),
    _run(2, "00:00", "06:00"),
    _run(3, "00:00", "15:30"),
    _run(4, "00:00", "20:00", "cancelled"),
    _run(5, "00:00", "20:00", branch="dev"),
]
JOBS = {1: _jobs(1, "16:08"), 3: _jobs(3, "14:50")}


@pytest.fixture
def swarm():
    import fakeredis

    store = SimpleNamespace(redis=fakeredis.FakeRedis(decode_responses=True))
    ledger = SimpleNamespace(added=[])
    ledger.followup = lambda slug, text: ledger.added.append((slug, text))
    return store, SimpleNamespace(repo="/repo"), ledger


def _gh(calls):
    def run(command, **kwargs):
        calls.append(command[2])
        if "/jobs" in command[2]:
            run_id = int(command[2].split("/runs/")[1].split("/")[0])
            return subprocess.CompletedProcess(command, 0, "".join(json.dumps(j) + "\n" for j in JOBS[run_id]), "")
        return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(r) for r in RUNS), "")

    return run


def test_a_run_over_fifteen_minutes_to_gate_required_is_filed_once_in_plain_words(swarm):
    store, config, ledger = swarm
    calls = []
    actions = defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls))
    assert len(ledger.added) == 1
    slug, text = ledger.added[0]
    assert slug == "sw"
    assert "16 minutes 8 seconds from push to Gate Required" in text
    assert "mutation" in text and "14 minutes 4 seconds against 8 minutes" in text
    assert URL.format(1) in text
    assert ledger_comments.problems(text, "item") == []
    assert actions == ["filed a ledger follow up for a pull request Tests run over fifteen minutes"]
    assert sum("/jobs" in c for c in calls) == 2

    later = NOW_MS + defects.REFRESH_MS
    assert defects.refresh("sw", config, store, ledger, later, run=_gh(calls)) == []
    assert len(ledger.added) == 1
    assert sum("/jobs" in c for c in calls) == 2


def test_the_pass_reads_github_at_most_once_per_refresh_interval(swarm):
    store, config, ledger = swarm
    calls = []
    defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls))
    read = len(calls)
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS - 1, run=_gh(calls))
    assert len(calls) == read


def test_a_failed_read_files_nothing_and_retries_next_interval(swarm, capsys):
    store, config, ledger = swarm

    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, "", "HTTP 502")

    assert defects.refresh("sw", config, store, ledger, NOW_MS, run=broken) == []
    assert ledger.added == []
    assert "HTTP 502" in capsys.readouterr().err
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([]))
    assert len(ledger.added) == 1


def test_a_refused_ledger_write_is_retried_next_interval(swarm):
    store, config, ledger = swarm
    refused = []

    def refuse(slug, text):
        refused.append(text)
        raise LedgerRefused("down")

    ledger.followup = refuse
    with pytest.raises(LedgerRefused):
        defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh([]))
    ledger.followup = lambda slug, text: ledger.added.append((slug, text))
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([]))
    assert len(refused) == 1 and len(ledger.added) == 1


def test_one_run_whose_jobs_cannot_be_read_does_not_hide_the_others(swarm):
    store, config, ledger = swarm
    calls = []
    healthy = _gh(calls)

    def run(command, **kwargs):
        if command[2].startswith("repos/{owner}/{repo}/actions/runs/1/"):
            raise subprocess.CalledProcessError(1, command, "", "HTTP 502")
        return healthy(command, **kwargs)

    defects.refresh("sw", config, store, ledger, NOW_MS, run=run)
    assert ledger.added == []
    assert any("/runs/3/jobs" in c for c in calls)
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=healthy)
    assert len(ledger.added) == 1


def test_a_run_without_a_finished_gate_is_no_defect():
    assert defects.defect(RUNS[0], JOBS[1][:1]) is None
