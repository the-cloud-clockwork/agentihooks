import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_branch_lanes as lanes

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
GROUP = "${{ " + lanes.LANE + " }}"
LANED = {"group": GROUP, "queue": "max", "cancel-in-progress": False}


def _workflow(concurrency=None, **jobs):
    workflow = {"name": "Replay", "on": {"workflow_dispatch": None}, "jobs": jobs or {"replay": {"runs-on": "x"}}}
    if concurrency is not None:
        workflow["concurrency"] = concurrency
    return workflow


def _repo(tmp_path, files):
    base = tmp_path / "base"
    (base / lanes.WORKFLOWS).mkdir(parents=True)
    (base / lanes.WORKFLOWS / "test.yml").write_text("name: Tests\n")
    head = tmp_path / "head"
    (head / lanes.WORKFLOWS).mkdir(parents=True)
    for name, body in {"test.yml": "name: Changed\n", **files}.items():
        (head / lanes.WORKFLOWS / name).write_text(body if isinstance(body, str) else yaml.safe_dump(body))
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head"]):
        subprocess.run(["git", "-C", str(head), *args], check=True)
    sha = subprocess.run(["git", "-C", str(head), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return base, head, sha


def test_the_lane_is_the_one_the_dev_workflows_share():
    shared = (ROOT / lanes.WORKFLOWS / "helm-kind.yml").read_text()
    assert lanes.LANE in shared


def test_a_laned_job_passes():
    assert lanes.lane_problem(LANED) is None
    assert lanes.lane_problem({"group": GROUP, "queue": "max"}) is None
    assert lanes.lane_problem({"group": GROUP.replace(" || ", " ||\n  "), "queue": "max"}) is None


@pytest.mark.parametrize(
    "block, problem",
    [
        (None, "has no shared hosted lane concurrency group"),
        (GROUP, "has no shared hosted lane concurrency group"),
        ({"group": "replay-${{ github.ref }}", "queue": "max"}, "has no shared hosted lane concurrency group"),
        ({"queue": "max"}, "has no shared hosted lane concurrency group"),
        ({"group": lanes.LANE, "queue": "max"}, "has no shared hosted lane concurrency group"),
        ({"group": GROUP + "-${{ github.ref }}", "queue": "max"}, "has no shared hosted lane concurrency group"),
        (
            {"group": "${{ github.event_name == 'push' && 'own' || " + lanes.LANE + " }}", "queue": "max"},
            "has no shared hosted lane concurrency group",
        ),
        ({"group": GROUP}, "queues on the lane without queue: max"),
        ({"group": GROUP, "queue": "single"}, "queues on the lane without queue: max"),
        ({"group": GROUP, "queue": "max", "cancel-in-progress": True}, "may cancel other runs on the shared lane"),
        (
            {"group": GROUP, "queue": "max", "cancel-in-progress": "${{ true }}"},
            "may cancel other runs on the shared lane",
        ),
    ],
)
def test_a_job_off_the_lane_is_refused_with_its_reason(block, problem):
    assert lanes.lane_problem(block) == problem


def test_a_workflow_level_lane_covers_every_job():
    assert lanes.problems(_workflow(LANED, a={}, b={})) == []


def test_a_job_level_block_overrides_the_workflow_lane():
    workflow = _workflow(LANED, a={}, b={"concurrency": {"group": "own"}})
    assert lanes.problems(workflow) == ["job b has no shared hosted lane concurrency group"]


def test_every_job_without_a_lane_is_named():
    workflow = _workflow(a={}, b={"concurrency": LANED}, c={"concurrency": {"group": GROUP}})
    assert lanes.problems(workflow) == [
        "job a has no shared hosted lane concurrency group",
        "job c queues on the lane without queue: max",
    ]


def test_a_workflow_without_jobs_has_no_problem_and_a_non_mapping_is_refused():
    assert lanes.problems({"name": "Empty", "jobs": None}) == []
    assert lanes.problems({"name": "Empty"}) == []
    assert lanes.problems(["not", "a", "workflow"]) == ["is not a workflow mapping"]
    assert lanes.problems(None) == ["is not a workflow mapping"]
    assert lanes.problems(_workflow(LANED, a=None, b="x")) == []
    assert lanes.problems(_workflow(a=None)) == ["job a has no shared hosted lane concurrency group"]
    assert lanes.problems({"jobs": ["a"]}) == ["has no jobs mapping"]
    assert lanes.problems({"jobs": "a"}) == ["has no jobs mapping"]


def test_a_file_that_does_not_parse_is_refused():
    assert lanes.grade("jobs: [unclosed") == ["does not parse as YAML"]
    assert lanes.grade(yaml.safe_dump(_workflow(LANED))) == []
    assert lanes.grade(yaml.safe_dump(_workflow())) == ["job replay has no shared hosted lane concurrency group"]


def test_only_workflows_absent_on_the_base_are_graded(tmp_path, monkeypatch):
    base, head, sha = _repo(
        tmp_path,
        {"plant.yml": _workflow(), "laned.yaml": _workflow(LANED), "notes.txt": "x", "other.yaml": _workflow()},
    )
    monkeypatch.chdir(head)
    assert lanes.branch_only(sha, base) == [
        f"{lanes.WORKFLOWS}/{name}" for name in ("laned.yaml", "other.yaml", "plant.yml")
    ]
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/{name}": ["job replay has no shared hosted lane concurrency group"]
        for name in ("other.yaml", "plant.yml")
    }


def test_a_head_whose_new_workflows_all_queue_on_a_lane_is_allowed(tmp_path, monkeypatch, capsys):
    base, head, sha = _repo(tmp_path, {"laned.yml": _workflow(LANED)})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    monkeypatch.setattr(lanes, "cancel", lambda *a: pytest.fail("nothing to cancel"))
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 0
    assert capsys.readouterr().out == "Every workflow that exists only on this head queues on a shared hosted lane.\n"


def test_a_refused_head_is_red_names_each_reason_and_cancels_its_runs(tmp_path, monkeypatch, capsys):
    base, head, sha = _repo(tmp_path, {"plant.yml": _workflow(), "laned.yml": _workflow(LANED)})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    calls = []
    monkeypatch.setattr(lanes, "cancel", lambda *args: calls.append(args) or {9, 7})
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 1
    assert calls == [("o/r", sha, {f"{lanes.WORKFLOWS}/plant.yml"}, 60.0)]
    assert capsys.readouterr().out == (
        f"Refused {lanes.WORKFLOWS}/plant.yml: job replay has no shared hosted lane concurrency group.\n"
        "Cancelled run 7.\nCancelled run 9.\n"
    )
    calls.clear()
    assert lanes.main(["--head", sha, "--repo", "o/r", "--wait", "3"]) == 1
    assert calls[0][3] == 3.0


def test_a_refused_head_without_a_repository_cancels_nothing(tmp_path, monkeypatch, capsys):
    base, head, sha = _repo(tmp_path, {"plant.yml": _workflow()})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    monkeypatch.setattr(lanes, "cancel", lambda *a: pytest.fail("no repository to cancel in"))
    assert lanes.main(["--head", sha]) == 1
    assert "Cancelled" not in capsys.readouterr().out


def _run(number, path, status="in_progress"):
    return {"id": number, "path": path, "status": status}


def test_cancel_stops_each_open_run_once_and_waits_until_every_refused_workflow_shows(monkeypatch):
    pages = [
        [_run(1, "a.yml"), _run(2, "keep.yml"), _run(5, "b.yml", "completed"), _run(6, "c.yml")],
        [_run(1, "a.yml"), _run(3, "b.yml@refs/heads/x"), _run(6, "c.yml"), _run(4, "a.yml", "completed")],
    ]
    calls, sleeps, stops = [], [], []
    monkeypatch.setattr(lanes, "api", lambda *args: calls.append(args) or {"workflow_runs": pages.pop(0)})
    monkeypatch.setattr(lanes, "stop", lambda repo, run: stops.append((repo, run)) or stops != [(repo, 1), (repo, 6)])
    monkeypatch.setattr(lanes.time, "sleep", sleeps.append)
    assert lanes.cancel("o/r", "abc", {"a.yml", "b.yml", "c.yml"}, 60) == {1, 3, 6}
    assert sleeps == [5.0]
    assert calls == [("repos/o/r/actions/runs?head_sha=abc&per_page=100",)] * 2
    assert stops == [("o/r", 1), ("o/r", 6), ("o/r", 3), ("o/r", 6)]


@pytest.mark.parametrize("code, stopped", [(0, True), (1, False)])
def test_stop_reports_whether_github_accepted_the_cancel(monkeypatch, code, stopped):
    seen = []
    monkeypatch.setattr(
        lanes.subprocess,
        "run",
        lambda argv, **kwargs: seen.append((argv, kwargs)) or subprocess.CompletedProcess(argv, code),
    )
    assert lanes.stop("o/r", 7) is stopped
    assert seen == [(["gh", "api", "-X", "POST", "repos/o/r/actions/runs/7/cancel"], {"capture_output": True})]


def test_cancel_gives_up_at_the_deadline_when_a_workflow_never_runs(monkeypatch):
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(lanes.time, "monotonic", lambda: next(clock))
    sleeps = []
    monkeypatch.setattr(lanes.time, "sleep", sleeps.append)
    listings = iter([{}, {}])
    monkeypatch.setattr(lanes, "api", lambda *a: next(listings))
    assert lanes.cancel("o/r", "abc", {"dispatch.yml"}, 2, step=0.5) == set()
    assert sleeps == [0.5]


def test_the_head_is_required():
    with pytest.raises(SystemExit):
        lanes.main([])


def test_api_and_git_read_their_commands(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"" if argv[0] == "gh" else "out")

    monkeypatch.setattr(lanes.subprocess, "run", run)
    assert lanes.api("-X", "POST", "x") == {}
    assert lanes.git("show", "y") == "out"
    assert seen == [
        (["gh", "api", "-X", "POST", "x"], {"capture_output": True, "check": True}),
        (["git", "show", "y"], {"capture_output": True, "text": True, "check": True}),
    ]


def test_the_workflow_grades_every_branch_push_with_the_dev_grader():
    workflow = yaml.safe_load((ROOT / lanes.WORKFLOWS / "branch-lanes.yml").read_text())
    assert workflow[True] == {"push": {"branches-ignore": ["dev", "main", "gh-readonly-queue/**", "diffcheck/**"]}}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read", "actions": "write"}
    steps = workflow["jobs"]["grade"]["steps"]
    assert steps[0]["with"]["ref"] == "dev"
    assert steps[-1]["run"] == 'python -m scripts.ci_branch_lanes --head "$GITHUB_SHA" --repo "$GITHUB_REPOSITORY"'
