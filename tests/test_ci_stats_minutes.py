import json
import sys
from datetime import UTC, datetime

import pytest

from scripts import ci_stats_minutes as stats


def test_measure_stats_counts_every_attempt_before_merge_and_excludes_other_jobs(monkeypatch):
    pulls = [
        {"number": 1, "merged_at": "2026-10-25T10:00:00Z", "head": {"ref": "first"}},
        {"number": 2, "merged_at": "2026-10-25T11:00:00Z", "head": {"ref": "second"}},
        {"number": 3, "merged_at": None, "head": {"ref": "open"}},
    ]
    runs = [
        {"id": 10, "head_sha": "old", "created_at": "2026-10-25T09:00:00Z"},
        {"id": 20, "head_sha": "new", "created_at": "2026-10-25T10:01:00Z"},
    ]
    jobs = [
        {
            "id": index,
            "name": name,
            "conclusion": conclusion,
            "started_at": "2026-10-25T09:00:00Z",
            "completed_at": "2026-10-25T09:01:00Z",
        }
        for index, (name, conclusion) in enumerate(
            [("stats (0)", "success"), ("stats (0)", "cancelled"), ("plan", "success"), ("stats", "skipped")]
        )
    ]
    calls = []

    def rows(endpoint, key):
        calls.append(endpoint)
        if "/pulls?" in endpoint:
            return pulls
        return runs if "workflows/" in endpoint else jobs

    monkeypatch.setattr(stats, "rows", rows)
    result = stats.measure("owner/repo", 15, "2026-10-25T10:30:00Z", "2026-10-25T08:00:00Z")
    assert result["count"] == 1
    assert result["mean"] == result["median"] == result["total"] == 2
    assert result["pull_requests"][0]["pr"] == 1
    assert [job["run"] for job in result["pull_requests"][0]["jobs"]] == [10, 10]
    assert len(calls) == 3


@pytest.mark.parametrize("explicit", [False, True])
def test_measurement_command_writes_rows_and_defaults_an_empty_cutoff_to_now(tmp_path, monkeypatch, capsys, explicit):
    calls = []

    class Clock:
        @staticmethod
        def now(zone):
            assert zone is UTC
            return datetime(2026, 10, 25, 12, tzinfo=UTC)

    def measure(repo, count, until, since):
        calls.append((repo, count, until, since))
        return {"count": 1, "mean": 2.2, "pull_requests": [{"pr": 1, "merged_at": until, "jobs": [], "minutes": 2.2}]}

    monkeypatch.setattr(stats, "datetime", Clock)
    monkeypatch.setattr(stats, "measure", measure)
    monkeypatch.chdir(tmp_path)
    cutoff = "2026-10-25T10:00:00Z" if explicit else ""
    arguments = ["stats-minutes", "--repo", "owner/repo", "--until", cutoff]
    if explicit:
        arguments.extend(["--count", "2", "--since", "2026-10-25T08:00:00Z", "--out", "report.json"])
    monkeypatch.setattr(sys, "argv", arguments)
    stats.main()
    expected = cutoff or "2026-10-25T12:00:00Z"
    assert calls == [("owner/repo", 2 if explicit else 15, expected, "2026-10-25T08:00:00Z" if explicit else "")]
    output = tmp_path / ("report.json" if explicit else "ci-stats-minutes.json")
    result = json.loads(output.read_text())
    assert result["pull_requests"][0]["pr"] == 1
    printed = capsys.readouterr().out
    assert f"1\t{expected}\t0\t2.2\n" in printed
    assert json.loads(printed.splitlines()[-1]) == {"count": 1, "mean": 2.2}
