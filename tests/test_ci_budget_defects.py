import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts.ci_budget import defects
from scripts.swarm.store import PREFIX
from scripts.swarm_ledger import ledger_comments

pytestmark = pytest.mark.xdist_group("fakeredis")

CI_SPEED_READ = True
NOW_MS = 1_791_540_000_000
URL = "https://github.com/o/r/actions/runs/{}/job/9"
TEXT = (
    "A pull request Tests run took 16 minutes 8 seconds from push to Gate Required, over the 15 minute budget."
    " Its slowest stage mutation took 14 minutes 4 seconds against 8 minutes. " + URL.format(1)
)


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


def _job(name, created, completed, run_id=1):
    return {
        "name": name,
        "created_at": f"2026-10-09T07:{created}Z",
        "completed_at": f"2026-10-09T07:{completed}Z",
        "conclusion": "success",
        "html_url": URL.format(run_id),
    }


def _jobs(run_id, gate_end):
    return [
        _job("mutation", "00:05", "14:09", run_id),
        _job("split", "00:05", "00:20", run_id),
        _job("Gate — Required", "14:10", gate_end, run_id),
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


def _gh(calls, runs=RUNS):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "/jobs" in command[2]:
            run_id = int(command[2].split("/runs/")[1].split("/")[0])
            return subprocess.CompletedProcess(command, 0, "".join(json.dumps(j) + "\n" for j in JOBS[run_id]), "")
        status = command[2].split("status=")[1].split("&")[0]
        lines = [json.dumps(r) for r in runs if r.get("conclusion") == status]
        return subprocess.CompletedProcess(command, 0, "\n".join(lines), "")

    return run


def _job_reads(calls):
    return [command[2] for command, _ in calls if "/jobs" in command[2]]


def test_keys_sit_under_the_swarm_prefix():
    assert defects.key("sw") == f"{PREFIX}:sw:ci-budget"
    assert defects.key("sw", "seen", "1") == f"{PREFIX}:sw:ci-budget:seen:1"


def test_a_run_over_fifteen_minutes_to_gate_required_is_filed_once_in_plain_words(swarm):
    store, config, ledger = swarm
    calls = []
    actions = defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls))
    assert ledger.added == [("sw", TEXT)]
    assert ledger_comments.problems(TEXT, "item") == []
    assert actions == ["filed a ledger follow up for a pull request Tests run over fifteen minutes"]
    assert 0 < store.redis.ttl(defects.key("sw", "seen", "1")) <= defects.SEEN_TTL_S
    assert store.redis.exists(defects.key("sw", "seen", "3"))
    assert _job_reads(calls) == [
        "repos/{owner}/{repo}/actions/runs/1/jobs?per_page=100",
        "repos/{owner}/{repo}/actions/runs/3/jobs?per_page=100",
    ]

    later = NOW_MS + defects.REFRESH_MS
    assert defects.refresh("sw", config, store, ledger, later, run=_gh(calls)) == []
    assert len(ledger.added) == 1
    assert len(_job_reads(calls)) == 2


def test_github_is_read_from_the_repository_over_the_window(swarm):
    store, config, ledger = swarm
    calls = []
    defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls))
    since = datetime.fromtimestamp(NOW_MS / 1000 - defects.WINDOW_S, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [(command, kwargs) for command, kwargs in calls if "/jobs" not in command[2]]
    assert runs and all(f"created=>={since}&" in command[2] and kwargs["cwd"] == "/repo" for command, kwargs in runs)
    command, kwargs = next((command, kwargs) for command, kwargs in calls if "/jobs" in command[2])
    assert command == [
        "gh",
        "api",
        "repos/{owner}/{repo}/actions/runs/1/jobs?per_page=100",
        "--paginate",
        "--jq",
        defects.JOBS_JQ,
    ]
    assert kwargs == {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60}


def test_the_pass_reads_github_at_most_once_per_refresh_interval(swarm):
    store, config, ledger = swarm
    calls = []
    defects.refresh("sw", config, store, ledger, defects.REFRESH_MS, run=_gh(calls))
    read = len(calls)
    assert read
    defects.refresh("sw", config, store, ledger, 2 * defects.REFRESH_MS - 1, run=_gh(calls))
    assert len(calls) == read


def test_a_run_of_exactly_fifteen_minutes_is_inside_budget(swarm):
    store, config, ledger = swarm
    calls = []
    defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh(calls, [_run(7, "00:00", "15:00")]))
    assert _job_reads(calls) == []
    assert defects.defect(_run(1, "00:00", "20:00"), _jobs(1, "15:00")) is None


def test_the_gate_itself_is_never_named_the_slowest_stage():
    jobs = [_job("kind-due", "00:05", "00:20"), _job("Gate — Required", "00:00", "16:00")]
    text = defects.defect(_run(1, "00:00", "16:00"), jobs)
    assert "slowest stage kind-due took 0 minutes 15 seconds against 1 minutes." in text


def test_a_slowest_stage_without_a_budget_says_so():
    jobs = [_job("brand-new", "00:05", "14:00"), _job("Gate — Required", "14:00", "16:00")]
    text = defects.defect(_run(1, "00:00", "16:00"), jobs)
    assert "slowest stage brand-new took 13 minutes 55 seconds with no budget." in text


def test_a_skipped_gate_still_ends_the_measured_run():
    gate = {**_job("Gate — Required", "14:10", "16:08"), "conclusion": "skipped"}
    assert defects.defect(RUNS[0], [*JOBS[1][:2], gate]) == TEXT


def test_a_failed_read_files_nothing_and_retries_next_interval(swarm, capsys):
    store, config, ledger = swarm

    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, "", "HTTP 502")

    assert defects.refresh("sw", config, store, ledger, NOW_MS, run=broken) == []
    assert ledger.added == []
    assert "ci budget skipped reading Tests runs: HTTP 502" in capsys.readouterr().err
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([]))
    assert len(ledger.added) == 1


def test_a_followup_the_ledger_refuses_is_not_sent_again(swarm):
    store, config, ledger = swarm
    refused = []

    def refuse(slug, text):
        refused.append(text)
        return False

    ledger.followup = refuse
    assert defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh([])) == []
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([]))
    assert len(refused) == 1


def test_a_followup_that_fails_to_reach_the_ledger_is_retried_next_interval(swarm):
    store, config, ledger = swarm

    def unreachable(slug, text):
        raise ConnectionRefusedError("ledger down")

    ledger.followup = unreachable
    defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh([]))
    ledger.followup = lambda slug, text: ledger.added.append((slug, text))
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=_gh([]))
    assert len(ledger.added) == 1


def test_a_malformed_run_record_does_not_stop_the_pass(swarm, capsys):
    store, config, ledger = swarm
    broken = {"id": 9, "event": "pull_request", "status": "completed", "conclusion": "success", "head_branch": "e"}
    defects.refresh("sw", config, store, ledger, NOW_MS, run=_gh([], [broken, *RUNS]))
    assert len(ledger.added) == 1
    assert "ci budget skipped one Tests run: 'updated_at'" in capsys.readouterr().err


def test_one_run_whose_jobs_cannot_be_read_does_not_hide_the_others(swarm, capsys):
    store, config, ledger = swarm
    calls = []
    healthy = _gh(calls)

    def run(command, **kwargs):
        if command[2].startswith("repos/{owner}/{repo}/actions/runs/1/"):
            raise subprocess.CalledProcessError(1, command, "", "HTTP 502")
        return healthy(command, **kwargs)

    defects.refresh("sw", config, store, ledger, NOW_MS, run=run)
    assert ledger.added == []
    assert "/runs/3/jobs" in _job_reads(calls)[-1]
    assert "ci budget skipped one Tests run: HTTP 502" in capsys.readouterr().err
    defects.refresh("sw", config, store, ledger, NOW_MS + defects.REFRESH_MS, run=healthy)
    assert len(ledger.added) == 1


def test_a_run_without_a_finished_gate_is_no_defect():
    assert defects.defect(RUNS[0], JOBS[1][:2]) is None
