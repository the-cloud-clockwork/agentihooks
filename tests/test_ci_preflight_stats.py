import subprocess

import pytest

from scripts import ci_preflight_stats as stats

PULLS = "pulls?state=closed&base=dev&per_page=100&sort=updated&direction=desc&page="
RUNS = "actions/workflows/mutation-preflight.yml/runs?event=push&per_page=100&branch="


def _pull(number, ref, created, merged):
    return {"number": number, "head": {"ref": ref}, "created_at": created, "merged_at": merged}


def _commits(*dates):
    return [{"commit": {"committer": {"date": date}}} for date in dates]


def _run(number, created):
    return {"id": number, "created_at": created}


def _jobs(mutation):
    jobs = [{"name": "plan", "conclusion": "success"}]
    return {"jobs": jobs + ([{"name": "mutation", "conclusion": mutation}] if mutation else [])}


@pytest.fixture
def github(monkeypatch):
    answers = {
        f"{PULLS}1": [
            _pull(3, "c", "2026-10-11T12:30:00Z", "2026-10-11T12:45:00Z"),
            _pull(4, "d", "2026-10-11T09:00:00Z", None),
            _pull(1, "a", "2026-10-11T10:20:00Z", "2026-10-11T10:40:00Z"),
        ],
        f"{PULLS}2": [_pull(2, "b", "2026-10-11T11:30:00Z", "2026-10-11T11:50:00Z")],
        "pulls/1/commits?per_page=100": _commits("2026-10-11T10:00:00Z"),
        "pulls/2/commits?per_page=100": _commits("2026-10-11T11:20:00Z", "2026-10-11T11:10:00Z"),
        "pulls/3/commits?per_page=100": _commits("2026-10-11T12:05:00Z"),
        f"{RUNS}a": {
            "workflow_runs": [
                _run(16, "2026-10-11T10:40:00Z"),
                _run(14, "2026-10-11T10:50:00Z"),
                _run(12, "2026-10-11T10:10:00Z"),
                _run(11, "2026-10-11T10:05:00Z"),
                _run(15, "2026-10-11T10:00:00Z"),
                _run(13, "2026-10-11T09:00:00Z"),
            ]
        },
        f"{RUNS}b": {"workflow_runs": [_run(22, "2026-10-11T11:25:00Z"), _run(21, "2026-10-11T11:15:00Z")]},
        f"{RUNS}c": {"workflow_runs": [_run(31, "2026-10-11T12:10:00Z")]},
    }
    mutations = {11: "success", 12: "skipped", 15: "skipped", 16: "skipped", 21: "failure", 22: None, 31: "cancelled"}
    for run, mutation in mutations.items():
        answers[f"actions/runs/{run}/jobs"] = _jobs(mutation)
    monkeypatch.setattr(stats, "api", lambda path: answers[path])


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["stats", *argv])
    code = stats.main()
    return code, capsys.readouterr().out


def test_before_takes_the_last_merges_before_the_time_with_every_preflight_in_their_window(github, monkeypatch, capsys):
    assert _main(monkeypatch, capsys, "before", "2026-10-11T12:00:00Z", "2", "2") == (
        0,
        "pull request 1 open after 20.0 min, 4 preflights, 1 mutated: "
        "16:skipped 12:skipped 11:success 15:skipped\n"
        "pull request 2 open after 20.0 min, 2 preflights, 1 mutated: 22:absent 21:failure\n"
        "pull requests 2\n"
        "median preflight runs per merged pull request 3.0\n"
        "median preflight runs that started the mutation job 1.0\n"
        "median first commit to pull request open minutes 20.0\n",
    )


def test_before_excludes_a_merge_at_the_time_and_keeps_only_the_last_count(github, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "before", "2026-10-11T11:50:00Z", "5", "2")
    assert (code, out.splitlines()[0].split(",")[0], out.splitlines()[1]) == (
        0,
        "pull request 1 open after 20.0 min",
        "pull requests 1",
    )
    code, out = _main(monkeypatch, capsys, "before", "2026-10-11T13:00:00Z", "1", "2")
    assert out.startswith("pull request 3 open after 25.0 min, 1 preflights, 1 mutated: 31:cancelled\n")


def test_after_takes_the_first_merges_whose_first_commit_follows_the_time(github, monkeypatch, capsys):
    assert _main(monkeypatch, capsys, "after", "2026-10-11T10:30:00Z", "2", "2") == (
        0,
        "pull request 2 open after 20.0 min, 2 preflights, 1 mutated: 22:absent 21:failure\n"
        "pull request 3 open after 25.0 min, 1 preflights, 1 mutated: 31:cancelled\n"
        "pull requests 2\n"
        "median preflight runs per merged pull request 1.5\n"
        "median preflight runs that started the mutation job 1.0\n"
        "median first commit to pull request open minutes 22.5\n",
    )


def test_after_stops_at_the_count_and_excludes_a_first_commit_at_the_time(github, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "after", "2026-10-11T10:30:00Z", "1", "2")
    assert out.splitlines()[1] == "pull requests 1"
    code, out = _main(monkeypatch, capsys, "after", "2026-10-11T11:10:00Z", "5", "2")
    assert out.startswith("pull request 3 ")
    assert out.splitlines()[1] == "pull requests 1"


def test_only_the_requested_pages_are_read(github, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "before", "2026-10-11T12:00:00Z", "5", "1")
    assert out.splitlines()[0].startswith("pull request 1 ")
    assert out.splitlines()[1] == "pull requests 1"


def test_no_matching_merge_reports_and_fails(github, monkeypatch, capsys):
    assert _main(monkeypatch, capsys, "before", "2026-10-11T09:00:00Z", "5", "2") == (
        1,
        "no merged pull requests match\n",
    )


def test_an_unknown_mode_is_refused(github, monkeypatch, capsys):
    with pytest.raises(SystemExit) as refused:
        _main(monkeypatch, capsys, "during", "2026-10-11T12:00:00Z", "2", "2")
    assert refused.value.code == 2
    assert "invalid choice: 'during'" in capsys.readouterr().err


def test_the_api_reads_the_repository_through_gh(monkeypatch):
    seen = []

    def run(command, **options):
        seen.append((command, options))
        return subprocess.CompletedProcess(command, 0, b'{"a": 1}', b"")

    monkeypatch.setattr(stats.subprocess, "run", run)
    assert stats.api("x") == {"a": 1}
    assert seen == [(["gh", "api", "repos/{owner}/{repo}/x"], {"capture_output": True, "check": True})]
