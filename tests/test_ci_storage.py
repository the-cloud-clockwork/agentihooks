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
BRANCHES = frozenset({"dev", "open"})
MB = ci_storage.MB


def artifact(id, name="a", branch="dev", hours=48, size=10, **state):
    return {
        "id": id,
        "name": name,
        "size_in_bytes": size,
        "expired": state.get("expired", False),
        "created_at": (state.get("now", NOW) - timedelta(hours=hours)).isoformat(),
        "workflow_run": {"head_branch": branch, "id": 1000 + id},
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
        artifact(6, branch="gone", hours=1),
    ]
    doomed, young = ci_storage.plan(found, BRANCHES, set(), NOW, KEEP)
    assert ids(doomed) == [1, 5]
    assert ids(young) == [6]


def test_the_newest_artifact_of_a_successful_run_survives_a_newer_failed_one():
    found = [artifact(1, hours=90), artifact(2, hours=80), artifact(3, hours=70), artifact(4, branch="pr", hours=60)]
    doomed, _ = ci_storage.plan(found, BRANCHES, {1002, 1004}, NOW, KEEP)
    assert ids(doomed) == [1, 4]


def test_expired_artifacts_are_doomed_after_the_live_ones():
    found = [artifact(1, expired=True), artifact(2), artifact(3, branch="pr")]
    doomed, young = ci_storage.plan(found, BRANCHES, set(), NOW, KEEP)
    assert ids(doomed) == [3, 1]
    assert young == []


def test_the_keep_window_is_exact():
    found = [artifact(1, branch="pr", hours=24), artifact(2, branch="pr", hours=24.01)]
    doomed, young = ci_storage.plan(found, BRANCHES, set(), NOW, KEEP)
    assert ids(doomed) == [2]
    assert ids(young) == [1]


def test_a_docker_build_record_is_never_kept():
    found = [artifact(1, name="o~r~ABC123.dockerbuild"), artifact(2, name="o~r~ABC123.dockerbuildx")]
    doomed, _ = ci_storage.plan(found, BRANCHES, {1001, 1002}, NOW, KEEP)
    assert ids(doomed) == [1]


def test_an_artifact_without_a_run_is_not_kept():
    found = [{**artifact(1), "workflow_run": None}, {**artifact(2, hours=1), "workflow_run": None}]
    doomed, young = ci_storage.plan(found, BRANCHES, set(), NOW, KEEP)
    assert ids(doomed) == [1]
    assert ids(young) == [2]


@pytest.fixture(autouse=True)
def waits(monkeypatch):
    slept = []
    monkeypatch.setattr(ci_storage.time, "sleep", slept.append)
    return slept


class Gh:
    def __init__(self, routes, failing=(), missing=(), flaky=(), hung=()):
        self.routes, self.failing, self.missing, self.calls = routes, set(failing), set(missing), []
        self.flaky, self.hung = set(flaky), set(hung)

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if "DELETE" in command:
            artifact_id = int(command[-1].rsplit("/", 1)[1])
            if artifact_id in self.hung:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if artifact_id in self.missing:
                return SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
            failed = artifact_id in self.failing
            return SimpleNamespace(returncode=int(failed), stdout="", stderr="gh: boom (HTTP 502)" if failed else "")
        path = next(part for part in command if "/" in part and not part.startswith("."))
        if path in self.flaky:
            self.flaky.discard(path)
            raise subprocess.CalledProcessError(1, command, "", "gh: Bad Gateway (HTTP 502)")
        if path in self.hung:
            self.hung.discard(path)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if self.routes.get(path) is None:
            raise subprocess.CalledProcessError(1, command, "", "gh: Server Error (HTTP 500)")
        jq = command[command.index("--jq") + 1]
        found = self.routes[path] if jq == ".[]" else self.routes[path][jq[1:-2]]
        return SimpleNamespace(returncode=0, stdout="".join(json.dumps(row) + "\n" for row in found))


def test_rows_paginates_reads_json_lines_and_fails_loudly(monkeypatch):
    gh = Gh({"orgs/o/repos?per_page=100": [{"a": 1}, {"b": 2}], "x/y": {"artifacts": [{"c": 3}]}})
    monkeypatch.setattr(subprocess, "run", gh)
    assert ci_storage.rows("orgs/o/repos?per_page=100", None) == [{"a": 1}, {"b": 2}]
    assert ci_storage.first_page("x/y", "artifacts") == [{"c": 3}]
    assert gh.calls[0] == (
        ["gh", "api", "--paginate", "orgs/o/repos?per_page=100", "--jq", ".[]"],
        {"capture_output": True, "text": True, "check": True, "timeout": 300},
    )
    assert gh.calls[1][0] == ["gh", "api", "x/y", "--jq", ".artifacts[]"]
    with pytest.raises(subprocess.CalledProcessError):
        ci_storage.rows("x/missing", None)
    assert [command[3] for command, _ in gh.calls[2:]] == ["x/missing"] * 3


def test_a_failed_or_hung_read_is_retried_after_a_growing_wait(monkeypatch, waits):
    gh = Gh({"x/y": [{"c": 3}]}, flaky={"x/y"})
    monkeypatch.setattr(subprocess, "run", gh)
    assert ci_storage.rows("x/y", None) == [{"c": 3}]
    assert len(gh.calls) == 2
    assert waits == [1]
    with pytest.raises(subprocess.CalledProcessError):
        ci_storage.rows("x/missing", None)
    assert waits == [1, 1, 2]
    gh.hung.add("x/y")
    assert ci_storage.rows("x/y", None) == [{"c": 3}]
    assert waits == [1, 1, 2, 1]


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


def test_passed_runs_reads_the_successful_runs_of_each_branch(monkeypatch):
    gh = Gh(
        {
            "repos/o/r/actions/runs?branch=a%20b&status=success&per_page=100": {"workflow_runs": [{"id": 1}]},
            "repos/o/r/actions/runs?branch=dev&status=success&per_page=100": {"workflow_runs": [{"id": 2}, {"id": 3}]},
        }
    )
    monkeypatch.setattr(subprocess, "run", gh)
    assert ci_storage.passed_runs("o/r", {"dev", "a b"}) == {1, 2, 3}
    assert gh.calls[0][0][:3] == ["gh", "api", "repos/o/r/actions/runs?branch=a%20b&status=success&per_page=100"]


def test_a_listing_the_api_refuses_falls_back_to_names_from_recent_runs(monkeypatch):
    routes = {
        "repos/o/r/actions/artifacts?per_page=100": None,
        "repos/o/r/actions/workflows?per_page=100": {"workflows": [{"id": 7}, {"id": 8}]},
        "repos/o/r/actions/workflows/7/runs?status=success&per_page=3": {"workflow_runs": [{"id": 70}, {"id": 71}]},
        "repos/o/r/actions/workflows/8/runs?status=success&per_page=3": {"workflow_runs": []},
        "repos/o/r/actions/runs/70/artifacts?per_page=100": {"artifacts": [{"name": "cov x"}, {"name": "b"}]},
        "repos/o/r/actions/runs/71/artifacts?per_page=100": {"artifacts": [{"name": "b"}]},
        "repos/o/r/actions/artifacts?name=b&per_page=100": {"artifacts": [{"id": 1}]},
        "repos/o/r/actions/artifacts?name=cov%20x&per_page=100": {"artifacts": [{"id": 2}, {"id": 3}]},
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
    monkeypatch.setattr(subprocess, "run", Gh({"repos/o/r/actions/artifacts?per_page=100": {"artifacts": [{"id": 9}]}}))
    assert ids(ci_storage.artifacts("o/r")) == [9]


def test_delete_counts_an_already_gone_artifact_as_deleted(monkeypatch):
    gh = Gh({}, failing={2}, missing={3}, hung={4})
    monkeypatch.setattr(subprocess, "run", gh)
    assert ci_storage.delete("o/r", {"id": 1}) is True
    assert ci_storage.delete("o/r", {"id": 2}) is False
    assert ci_storage.delete("o/r", {"id": 3}) is True
    assert ci_storage.delete("o/r", {"id": 4}) is False
    assert gh.calls[0] == (
        ["gh", "api", "--method", "DELETE", "repos/o/r/actions/artifacts/1"],
        {"capture_output": True, "text": True, "timeout": 60},
    )


def _org(found_a, found_b, unread=None):
    return {
        "orgs/o/repos?per_page=100": [
            {"full_name": "o/a", "visibility": "private", "archived": False, "default_branch": "dev"},
            {"full_name": "o/b", "visibility": "private", "archived": False, "default_branch": "dev"},
        ],
        "repos/o/a/actions/artifacts?per_page=100": {"artifacts": found_a},
        "repos/o/b/actions/artifacts?per_page=100": {"artifacts": found_b},
        "repos/o/a/pulls?state=open&per_page=100": None if unread == "o/a" else [],
        "repos/o/b/pulls?state=open&per_page=100": None if unread == "o/b" else [{"head": {"ref": "open"}}],
        "repos/o/a/actions/runs?branch=dev&status=success&per_page=100": {"workflow_runs": []},
        "repos/o/b/actions/runs?branch=dev&status=success&per_page=100": {"workflow_runs": []},
        "repos/o/b/actions/runs?branch=open&status=success&per_page=100": {"workflow_runs": []},
    }


def _args(**overrides):
    values = {"owner": "o", "keep_hours": 24, "floor_hours": 6, "limit_mb": 1, "budget": 3, "apply": True}
    return SimpleNamespace(**{**values, **overrides})


def test_prune_spends_one_budget_on_the_largest_artifacts_of_every_repository(monkeypatch):
    found_a = [
        artifact(1, branch="pr", size=3 * MB),
        artifact(2, branch="pr", size=MB),
        artifact(9, expired=True, size=MB),
    ]
    found_b = [artifact(3, branch="pr", size=2 * MB), artifact(4, branch="open", size=MB)]
    gh = Gh(_org(found_a, found_b), failing={2})
    monkeypatch.setattr(subprocess, "run", gh)
    result = ci_storage.prune(_args(), NOW)
    assert result == {
        "repositories": [
            {
                "repository": "o/a",
                "listed": 3,
                "live_bytes": 4 * MB,
                "deleted": 1,
                "deleted_bytes": 3 * MB,
                "failed": 1,
                "after_bytes": MB,
                "pending": 1,
            },
            {
                "repository": "o/b",
                "listed": 2,
                "live_bytes": 3 * MB,
                "deleted": 1,
                "deleted_bytes": 2 * MB,
                "failed": 0,
                "after_bytes": MB,
                "pending": 0,
            },
        ],
        "unread": [],
    }
    deleted = sorted(command[-1] for command, _ in gh.calls if "DELETE" in command)
    assert deleted == [
        "repos/o/a/actions/artifacts/1",
        "repos/o/a/actions/artifacts/2",
        "repos/o/b/actions/artifacts/3",
    ]


def test_over_the_organisation_limit_young_artifacts_go_oldest_first_down_to_the_floor(monkeypatch):
    found_a = [
        artifact(1, branch="pr", hours=20, size=MB),
        artifact(2, branch="pr", hours=7, size=MB),
        artifact(5, branch="pr", hours=48, size=MB),
    ]
    found_b = [artifact(3, branch="pr", hours=10, size=MB), artifact(4, branch="pr", hours=5, size=MB)]
    gh = Gh(_org(found_a, found_b))
    monkeypatch.setattr(subprocess, "run", gh)

    def deleted(limit):
        gh.calls.clear()
        result = ci_storage.prune(_args(limit_mb=limit, budget=10), NOW)
        return sorted(int(command[-1].rsplit("/", 1)[1]) for command, _ in gh.calls if "DELETE" in command), result

    assert deleted(1)[0] == [1, 2, 3, 5]
    assert deleted(2)[0] == [1, 3, 5]
    assert deleted(3)[0] == [1, 5]
    picked, result = deleted(4)
    assert picked == [5]
    assert sum(row["after_bytes"] for row in result["repositories"]) == 4 * MB


def test_the_floor_window_is_exact(monkeypatch):
    found_a = [artifact(1, branch="pr", hours=6, size=MB), artifact(2, branch="pr", hours=6.01, size=MB)]
    gh = Gh(_org(found_a, []))
    monkeypatch.setattr(subprocess, "run", gh)
    ci_storage.prune(_args(limit_mb=0), NOW)
    assert [command[-1] for command, _ in gh.calls if "DELETE" in command] == ["repos/o/a/actions/artifacts/2"]


def test_a_repository_that_cannot_be_read_is_reported_and_the_rest_still_pruned(monkeypatch):
    found = [artifact(1, branch="pr", size=MB)]
    gh = Gh(_org(found, found, unread="o/a"))
    monkeypatch.setattr(subprocess, "run", gh)
    result = ci_storage.prune(_args(), NOW)
    assert result["unread"] == ["o/a"]
    assert [row["repository"] for row in result["repositories"]] == ["o/b"]
    assert result["repositories"][0]["deleted"] == 1


def test_a_dry_prune_deletes_nothing_and_reports_the_plan(monkeypatch):
    found_a = [artifact(1, branch="pr", size=3 * MB), artifact(2, branch="pr", size=MB)]
    gh = Gh(_org(found_a, []))
    monkeypatch.setattr(subprocess, "run", gh)
    result = ci_storage.prune(_args(apply=False, budget=1, keep_hours=1, floor_hours=0.5, limit_mb=5), NOW)
    assert not [command for command, _ in gh.calls if "DELETE" in command]
    row = result["repositories"][0]
    assert (row["deleted"], row["deleted_bytes"], row["pending"], row["after_bytes"]) == (1, 3 * MB, 1, MB)
    assert result["repositories"][1]["listed"] == 0


def _row(name="o/r", failed=0, after=0, pending=0):
    return {"repository": name, "failed": failed, "after_bytes": after, "pending": pending}


def test_the_verdict_judges_the_organisation_total_against_the_limit():
    rows = [_row("o/a", after=1.5 * MB), _row("o/b", after=MB)]
    assert ci_storage.verdict({"repositories": rows, "unread": []}, 2 * MB) == (
        ["::error title=Kept artifacts over the limit::billed repositories keep 2.5 MB that no rule may delete"],
        1,
    )
    rows[1]["pending"] = 4
    assert ci_storage.verdict({"repositories": rows, "unread": []}, 2 * MB) == (
        ["::warning title=Pruning continues::billed repositories keep 2.5 MB with 4 deletions left for the next run"],
        0,
    )
    assert ci_storage.verdict({"repositories": rows, "unread": []}, 2.5 * MB) == ([], 0)


def test_the_verdict_fails_on_unread_repositories_and_failed_deletions():
    result = {"repositories": [_row("o/a", failed=2), _row("o/b", failed=1)], "unread": ["o/c"]}
    assert ci_storage.verdict(result, MB) == (
        [
            "::error title=Artifacts not listed::o/c could not be listed",
            "::error title=Artifact deletion failed::2 deletions failed in o/a",
            "::error title=Artifact deletion failed::1 deletions failed in o/b",
        ],
        1,
    )


def test_main_writes_the_report_and_prints_each_repository(monkeypatch, tmp_path, capfd):
    now = datetime.now(UTC)
    found_a = [artifact(1, branch="pr", size=3 * MB, now=now), artifact(2, branch="dev", size=MB, now=now)]
    seen = {}
    real = ci_storage.plan

    def plan(found, branches, passed, now, keep):
        seen.update(keep=keep, aware=now.tzinfo is UTC)
        return real(found, branches, passed, now, keep)

    monkeypatch.setattr(subprocess, "run", Gh(_org(found_a, [])))
    monkeypatch.setattr(ci_storage, "plan", plan)
    report = tmp_path / "report.json"
    assert ci_storage.main(["--owner", "o", "--report", str(report)]) == 0
    assert seen == {"keep": timedelta(hours=24), "aware": True}
    assert report.read_text() == json.dumps(json.loads(report.read_text()), indent=2) + "\n"
    assert json.loads(report.read_text())["repositories"][0]["deleted"] == 1
    out = capfd.readouterr().out
    assert "o/a: 2 listed, 4.0 MB live, 1 deleted (3.0 MB), 1.0 MB kept, 0 pending\n" in out
    assert "o/b: 0 listed, 0.0 MB live, 0 deleted (0.0 MB), 0.0 MB kept, 0 pending\n" in out


def test_main_options_reach_the_plan_and_the_verdict(monkeypatch, tmp_path, capfd):
    now = datetime.now(UTC)
    found = [artifact(1, branch="pr", size=3 * MB, hours=2, now=now)]
    monkeypatch.setattr(subprocess, "run", Gh(_org(found, found, unread="o/b")))
    argv = ["--owner", "o", "--keep-hours", "1", "--floor-hours", "0.5", "--limit-mb", "2", "--budget", "0"]
    assert ci_storage.main([*argv, "--report", str(tmp_path / "r.json")]) == 1
    assert capfd.readouterr().out.endswith(
        "::error title=Artifacts not listed::o/b could not be listed\n"
        "::warning title=Pruning continues::billed repositories keep 3.0 MB with 1 deletions left for the next run\n"
    )


def test_main_defaults_to_a_dry_run_that_reports_in_the_working_folder(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ci_storage, "prune", lambda args, now: seen.append(args) or {"repositories": [], "unread": []})
    monkeypatch.chdir(tmp_path)
    assert ci_storage.main(["--owner", "o"]) == 0
    assert ci_storage.main(["--owner", "o", "--apply"]) == 0
    assert ci_storage.main(["--owner", "o", "--floor-hours", "0.5", "--keep-hours", "1.5"]) == 0
    first, second, third = seen
    assert (third.floor_hours, third.keep_hours) == (0.5, 1.5)
    assert (first.owner, first.budget, first.limit_mb, first.floor_hours, first.apply, second.apply) == (
        "o",
        1500,
        1000,
        6,
        False,
        True,
    )
    assert json.loads((tmp_path / "storage-report.json").read_text()) == {"repositories": [], "unread": []}
    with pytest.raises(SystemExit):
        ci_storage.main([])


def test_the_workflow_prunes_hourly_with_its_own_app_token():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/actions-storage.yml").read_text())
    assert workflow[True]["schedule"] == [{"cron": "41 * * * *"}]
    steps = workflow["jobs"]["prune"]["steps"]
    token = next(step for step in steps if step.get("id") == "app-token")
    assert token["with"]["app-id"] == "${{ secrets.AGENTICORE_APP_ID }}"
    assert token["with"]["permission-actions"] == "write"
    assert "repositories" not in token["with"]
    assert any("python -m scripts.ci_storage" in step.get("run", "") and "--apply" in step["run"] for step in steps)
