import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import ci_storage

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
KEEP = timedelta(hours=24)
FLOOR = timedelta(hours=6)
BRANCHES = frozenset({"dev", "open"})


def artifact(id, name="a", branch="dev", hours=48, size=10, expired=False, now=NOW):
    return {
        "id": id,
        "name": name,
        "size_in_bytes": size,
        "expired": expired,
        "created_at": (now - timedelta(hours=hours)).isoformat(),
        "workflow_run": {"head_branch": branch},
    }


def ids(rows):
    return [row["id"] for row in rows]


def test_the_newest_artifact_per_name_on_a_kept_branch_survives_any_age():
    found = [
        artifact(1, hours=900),
        artifact(2, hours=500),
        artifact(3, name="b", hours=800),
        artifact(4, branch="open", hours=700),
        artifact(5, branch="gone", hours=600),
    ]
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 1000)
    assert ids(doomed) == [1, 5]
    assert total == 30


def test_superseded_artifacts_go_largest_first_and_expired_ones_last():
    found = [
        artifact(1, size=5),
        artifact(2, size=50),
        artifact(3, expired=True, size=70),
        artifact(4, branch="pr", size=20),
        artifact(5, size=1),
    ]
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 1000)
    assert ids(doomed) == [2, 4, 1, 3]
    assert total == 1


def test_an_artifact_inside_the_keep_window_waits():
    found = [artifact(1, branch="pr", hours=23), artifact(2, branch="pr", hours=24.5)]
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 1000)
    assert ids(doomed) == [2]
    assert total == 10


def test_over_the_limit_young_artifacts_go_oldest_first_down_to_the_floor():
    found = [
        artifact(1, branch="pr", hours=20, size=40),
        artifact(2, branch="pr", hours=10, size=40),
        artifact(3, branch="pr", hours=7, size=40),
        artifact(4, branch="pr", hours=5, size=40),
    ]
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 60)
    assert ids(doomed) == [1, 2, 3]
    assert total == 40
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 80)
    assert ids(doomed) == [1, 2]
    assert total == 80
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 160)
    assert doomed == []
    assert total == 160


def test_the_floor_and_keep_windows_are_exact():
    found = [artifact(1, branch="pr", hours=24, size=40), artifact(2, branch="pr", hours=6, size=40)]
    doomed, total = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 0)
    assert ids(doomed) == [1]
    assert total == 40


def test_an_artifact_without_a_run_is_not_kept():
    found = [{**artifact(1), "workflow_run": None}, {**artifact(2, hours=1), "workflow_run": None}]
    doomed, _ = ci_storage.plan(found, BRANCHES, NOW, KEEP, FLOOR, 1000)
    assert ids(doomed) == [1]


class Gh:
    def __init__(self, routes, failing=(), missing=()):
        self.routes, self.failing, self.missing, self.calls = routes, set(failing), set(missing), []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if "DELETE" in command:
            artifact_id = int(command[-1].rsplit("/", 1)[1])
            if artifact_id in self.missing:
                return SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
            failed = artifact_id in self.failing
            return SimpleNamespace(returncode=int(failed), stdout="", stderr="gh: boom (HTTP 502)" if failed else "")
        path = next(part for part in command if "/" in part and not part.startswith("."))
        if self.routes.get(path) is None:
            raise subprocess.CalledProcessError(1, command, "", "gh: Server Error (HTTP 500)")
        return SimpleNamespace(returncode=0, stdout="".join(json.dumps(row) + "\n" for row in self.routes[path]))


def test_rows_paginates_reads_json_lines_and_fails_loudly(monkeypatch):
    gh = Gh({"orgs/o/repos?per_page=100": [{"a": 1}, {"b": 2}], "x/y": [{"c": 3}]})
    monkeypatch.setattr(subprocess, "run", gh)
    assert ci_storage.rows("orgs/o/repos?per_page=100", None) == [{"a": 1}, {"b": 2}]
    assert ci_storage.rows("x/y", "artifacts", paginate=False) == [{"c": 3}]
    assert gh.calls[0] == (
        ["gh", "api", "--paginate", "orgs/o/repos?per_page=100", "--jq", ".[]"],
        {"capture_output": True, "text": True, "check": True, "timeout": 900},
    )
    assert gh.calls[1][0] == ["gh", "api", "x/y", "--jq", ".artifacts[]"]
    with pytest.raises(subprocess.CalledProcessError):
        ci_storage.rows("x/missing", None)


def test_only_unarchived_private_and_internal_repositories_are_billed(monkeypatch):
    repos = [
        {"full_name": "o/z", "visibility": "private", "archived": False},
        {"full_name": "o/pub", "visibility": "public", "archived": False},
        {"full_name": "o/old", "visibility": "private", "archived": True},
        {"full_name": "o/a", "visibility": "internal", "archived": False},
    ]
    monkeypatch.setattr(subprocess, "run", Gh({"orgs/o/repos?per_page=100": repos}))
    assert [repo["full_name"] for repo in ci_storage.billed("o")] == ["o/a", "o/z"]


def test_kept_branches_add_the_default_and_every_open_pull_request_head(monkeypatch):
    gh = Gh({"repos/o/r/pulls?state=open&per_page=100": [{"head": {"ref": "topic"}}]})
    monkeypatch.setattr(subprocess, "run", gh)
    branches = ci_storage.kept_branches({"full_name": "o/r", "default_branch": "trunk"})
    assert branches == {"dev", "main", "master", "trunk", "topic"}


def test_a_listing_the_api_refuses_falls_back_to_names_from_recent_runs(monkeypatch):
    routes = {
        "repos/o/r/actions/artifacts?per_page=100": None,
        "repos/o/r/actions/workflows?per_page=100": [{"id": 7}, {"id": 8}],
        "repos/o/r/actions/workflows/7/runs?status=success&per_page=3": [{"id": 70}, {"id": 71}],
        "repos/o/r/actions/workflows/8/runs?status=success&per_page=3": [],
        "repos/o/r/actions/runs/70/artifacts?per_page=100": [{"name": "cov x"}, {"name": "b"}],
        "repos/o/r/actions/runs/71/artifacts?per_page=100": [{"name": "b"}],
        "repos/o/r/actions/artifacts?name=b&per_page=100": [{"id": 1}],
        "repos/o/r/actions/artifacts?name=cov%20x&per_page=100": [{"id": 2}, {"id": 3}],
    }
    gh = Gh(routes)
    monkeypatch.setattr(subprocess, "run", gh)
    assert ids(ci_storage.artifacts("o/r")) == [1, 2, 3]
    runs = [
        command for command, _ in gh.calls if "repos/o/r/actions/workflows/7/runs?status=success&per_page=3" in command
    ]
    assert runs == [
        ["gh", "api", "repos/o/r/actions/workflows/7/runs?status=success&per_page=3", "--jq", ".workflow_runs[]"]
    ]


def test_a_listing_the_api_answers_is_used_whole(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Gh({"repos/o/r/actions/artifacts?per_page=100": [{"id": 9}]}))
    assert ids(ci_storage.artifacts("o/r")) == [9]


def test_delete_counts_an_already_gone_artifact_as_deleted(monkeypatch):
    gh = Gh({}, failing={2}, missing={3})
    monkeypatch.setattr(subprocess, "run", gh)
    assert ci_storage.delete("o/r", {"id": 1}) is True
    assert ci_storage.delete("o/r", {"id": 2}) is False
    assert ci_storage.delete("o/r", {"id": 3}) is True
    assert gh.calls[0] == (
        ["gh", "api", "--method", "DELETE", "repos/o/r/actions/artifacts/1"],
        {"capture_output": True, "text": True, "timeout": 60},
    )


def _org(found_a, found_b):
    return {
        "orgs/o/repos?per_page=100": [
            {"full_name": "o/a", "visibility": "private", "archived": False, "default_branch": "dev"},
            {"full_name": "o/b", "visibility": "private", "archived": False, "default_branch": "dev"},
        ],
        "repos/o/a/actions/artifacts?per_page=100": found_a,
        "repos/o/b/actions/artifacts?per_page=100": found_b,
        "repos/o/a/pulls?state=open&per_page=100": [],
        "repos/o/b/pulls?state=open&per_page=100": [{"head": {"ref": "open"}}],
    }


def _args(**overrides):
    values = {
        "owner": "o",
        "keep_hours": 24,
        "floor_hours": 6,
        "limit_mb": 1,
        "budget": 3,
        "apply": True,
    }
    return SimpleNamespace(**{**values, **overrides})


MB = ci_storage.MB


def test_prune_spends_one_budget_across_repositories_and_reports_each(monkeypatch):
    found_a = [
        artifact(1, branch="pr", size=3 * MB),
        artifact(2, branch="pr", size=MB),
        artifact(9, expired=True, size=MB),
    ]
    found_b = [artifact(3, branch="pr", size=2 * MB), artifact(4, branch="open", size=MB)]
    gh = Gh(_org(found_a, found_b), failing={2})
    monkeypatch.setattr(subprocess, "run", gh)
    report = ci_storage.prune(_args(), NOW)
    assert report == [
        {
            "repository": "o/a",
            "listed": 3,
            "live_bytes": 4 * MB,
            "deleted": 2,
            "deleted_bytes": 3 * MB + MB,
            "failed": 1,
            "after_bytes": 0,
            "pending": 0,
        },
        {
            "repository": "o/b",
            "listed": 2,
            "live_bytes": 3 * MB,
            "deleted": 0,
            "deleted_bytes": 0,
            "failed": 0,
            "after_bytes": 3 * MB,
            "pending": 1,
        },
    ]
    deleted = sorted(command[-1] for command, _ in gh.calls if "DELETE" in command)
    assert deleted == [
        "repos/o/a/actions/artifacts/1",
        "repos/o/a/actions/artifacts/2",
        "repos/o/a/actions/artifacts/9",
    ]


def test_a_dry_prune_deletes_nothing_and_reports_the_plan(monkeypatch):
    found_a = [artifact(1, branch="pr", size=3 * MB), artifact(2, branch="pr", size=MB)]
    gh = Gh(_org(found_a, []))
    monkeypatch.setattr(subprocess, "run", gh)
    report = ci_storage.prune(_args(apply=False, budget=1, keep_hours=1, floor_hours=0.5, limit_mb=5), NOW)
    assert not [command for command, _ in gh.calls if "DELETE" in command]
    assert report[0]["deleted"] == 1
    assert report[0]["deleted_bytes"] == 3 * MB
    assert report[0]["pending"] == 1
    assert report[0]["after_bytes"] == MB
    assert report[1]["listed"] == 0


def test_the_verdict_fails_on_errors_and_on_kept_bytes_no_rule_may_delete():
    row = {"repository": "o/r", "failed": 0, "after_bytes": 2.5 * MB, "pending": 0}
    assert ci_storage.verdict([row], 2 * MB) == (
        ["::error title=Kept artifacts over the limit::o/r keeps 2.5 MB that no rule may delete"],
        1,
    )
    assert ci_storage.verdict([{**row, "pending": 4}], 2 * MB) == (
        ["::warning title=Pruning continues::o/r keeps 2.5 MB with 4 deletions left for the next run"],
        0,
    )
    assert ci_storage.verdict([{**row, "failed": 2}], 3 * MB) == (
        ["::error title=Artifact deletion failed::2 deletions failed in o/r"],
        1,
    )
    assert ci_storage.verdict([{**row, "after_bytes": 2 * MB}], 2 * MB) == ([], 0)


def test_main_writes_the_report_and_prints_each_repository(monkeypatch, tmp_path, capfd):
    now = datetime.now(UTC)
    found_a = [artifact(1, branch="pr", size=3 * MB, now=now), artifact(2, branch="dev", size=MB, now=now)]
    seen = {}
    real = ci_storage.plan

    def plan(found, branches, now, keep, floor, limit):
        seen.update(keep=keep, floor=floor, limit=limit, aware=now.tzinfo is UTC)
        return real(found, branches, now, keep, floor, limit)

    monkeypatch.setattr(subprocess, "run", Gh(_org(found_a, [])))
    monkeypatch.setattr(ci_storage, "plan", plan)
    report = tmp_path / "report.json"
    assert ci_storage.main(["--owner", "o", "--report", str(report)]) == 0
    assert seen == {"keep": timedelta(hours=24), "floor": timedelta(hours=6), "limit": 300 * MB, "aware": True}
    assert json.loads(report.read_text())[0]["deleted"] == 1
    assert report.read_text().endswith("}\n]\n")
    out = capfd.readouterr().out
    assert "o/a: 2 listed, 4.0 MB live, 1 deleted (3.0 MB), 1.0 MB kept, 0 pending\n" in out
    assert "o/b: 0 listed, 0.0 MB live, 0 deleted (0.0 MB), 0.0 MB kept, 0 pending\n" in out


def test_main_options_reach_the_plan_and_the_verdict(monkeypatch, tmp_path, capfd):
    found_a = [artifact(1, branch="pr", size=3 * MB, hours=2, now=datetime.now(UTC))]
    monkeypatch.setattr(subprocess, "run", Gh(_org(found_a, [])))
    argv = ["--owner", "o", "--keep-hours", "1", "--floor-hours", "0.5", "--limit-mb", "2", "--budget", "0"]
    assert ci_storage.main([*argv, "--report", str(tmp_path / "r.json")]) == 0
    assert "::warning title=Pruning continues::o/a keeps 3.0 MB with 1 deletions left for the next run" in (
        capfd.readouterr().out
    )


def test_the_workflow_prunes_hourly_with_its_own_app_token():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/actions-storage.yml").read_text())
    assert workflow[True]["schedule"] == [{"cron": "41 * * * *"}]
    steps = workflow["jobs"]["prune"]["steps"]
    token = next(step for step in steps if step.get("id") == "app-token")
    assert token["with"]["app-id"] == "${{ secrets.AGENTICORE_APP_ID }}"
    assert token["with"]["permission-actions"] == "write"
    assert "repositories" not in token["with"]
    assert any("python -m scripts.ci_storage" in step.get("run", "") and "--apply" in step["run"] for step in steps)
