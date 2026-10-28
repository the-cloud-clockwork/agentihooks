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


CALLED = {
    "group": "${{ github.workflow == 'Replay' && " + lanes.LANE + " || format('replay-{0}', github.run_id) }}",
    "queue": "${{ github.workflow == 'Replay' && 'max' || 'single' }}",
}


def test_a_callable_workflow_takes_the_lane_on_its_own_runs_and_a_run_group_under_a_caller():
    assert lanes.problems(_workflow(CALLED)) == []
    loaded = yaml.safe_load((ROOT / lanes.WORKFLOWS / "ledger-load.yml").read_text())
    assert lanes.problems({**loaded, "on": {"workflow_dispatch": None}}) == []


@pytest.mark.parametrize(
    "change",
    [
        {"group": CALLED["group"].replace("'Replay'", "'Other'")},
        {"queue": CALLED["queue"].replace("'Replay'", "'Other'")},
        {"queue": "${{ github.workflow == 'Replay' && 'single' || 'max' }}"},
        {"queue": "max"},
        {"group": CALLED["group"].replace("github.run_id", "github.ref")},
        {"group": CALLED["group"].replace("'Replay' &&", "'Replay' ||")},
        {"cancel-in-progress": True},
    ],
)
def test_a_callable_lane_that_another_name_or_group_could_escape_is_refused(change):
    assert lanes.problems(_workflow({**CALLED, **change})) != []


def test_a_callable_lane_must_name_its_own_workflow():
    workflow = _workflow(CALLED)
    workflow["name"] = "Other"
    assert lanes.problems(workflow) == ["job replay has no shared hosted lane concurrency group"]


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


@pytest.mark.parametrize("name", ["replay.yml", "branch-lanes.yml"])
def test_removing_the_lane_from_an_existing_workflow_is_refused(tmp_path, monkeypatch, name):
    base, head, sha = _repo(tmp_path, {name: _workflow()})
    (base / lanes.WORKFLOWS / name).write_text(yaml.safe_dump(_workflow(LANED)))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/{name}": ["job replay has no shared hosted lane concurrency group"]
    }


def test_existing_dev_concurrency_is_preserved_while_job_steps_change(tmp_path, monkeypatch):
    trusted = {"group": "${{ github.workflow }}", "cancel-in-progress": False}
    updated = _workflow(trusted, replay={"runs-on": "x", "steps": [{"run": "echo changed"}]})
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(_workflow(trusted)))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


@pytest.mark.parametrize("trusted", [None, {"group": "existing-dev-policy"}])
def test_existing_job_concurrency_is_preserved_while_steps_change(tmp_path, monkeypatch, trusted):
    baseline = _workflow(replay={"runs-on": "x", "concurrency": trusted})
    updated = _workflow(replay={"runs-on": "x", "concurrency": trusted, "steps": [{"run": "echo changed"}]})
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


def test_unchanged_workflows_are_not_selected_as_edits(tmp_path, monkeypatch):
    workflow = _workflow(LANED)
    base, head, sha = _repo(tmp_path, {"replay.yml": workflow})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(workflow))
    (base / lanes.WORKFLOWS / "test.yml").write_text("name: Changed\n")
    monkeypatch.chdir(head)
    assert lanes.branch_only(sha, base, include_edits=True) == []


def test_unchanged_workflows_do_not_fetch_ancestry(tmp_path, monkeypatch):
    _, repo, head = _repo(tmp_path, {})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        lanes, "trusted_ancestor", lambda *_: pytest.fail("unchanged workflows must not fetch ancestry")
    )
    assert lanes.refused(head, repo) == {}


@pytest.mark.parametrize("change", ["override", "rename"])
def test_an_edit_cannot_escape_trusted_concurrency(tmp_path, monkeypatch, change):
    trusted = {"group": "${{ github.workflow == 'Replay' && 'hosted-lane-a' || github.run_id }}"}
    updated = _workflow(trusted)
    if change == "override":
        updated["jobs"]["replay"]["concurrency"] = None
    else:
        updated["name"] = "Evasion"
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(_workflow(trusted)))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/replay.yml": ["job replay has no shared hosted lane concurrency group"]
    }


def test_an_edited_workflow_cannot_add_an_uncapped_job(tmp_path, monkeypatch):
    baseline = _workflow(allowed={"runs-on": "x", "concurrency": LANED})
    updated = _workflow(allowed={"runs-on": "x", "concurrency": LANED}, escape={"runs-on": "x"})
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/replay.yml": ["job escape has no shared hosted lane concurrency group"]
    }


@pytest.mark.parametrize("added", [False, True])
@pytest.mark.parametrize("shallow", [False, True])
def test_unchanged_ancestor_jobs_stay_trusted_without_admitting_new_jobs(tmp_path, monkeypatch, added, shallow):
    inherited = _workflow(promote={"runs-on": "x", "steps": [{"run": "echo inherited"}]})
    _, repo, ancestor = _repo(tmp_path, {"replay.yml": inherited})
    monkeypatch.chdir(repo)
    lanes.git("checkout", "-qb", "branch")
    branch = _workflow(
        promote=inherited["jobs"]["promote"],
        proof={"runs-on": "x", "concurrency": LANED},
    )
    if added:
        branch["jobs"]["escape"] = {"runs-on": "x"}
    workflow = repo / lanes.WORKFLOWS / "replay.yml"
    workflow.write_text(yaml.safe_dump(branch))
    lanes.git("add", ".")
    lanes.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "branch")
    head = lanes.git("rev-parse", "HEAD").strip()
    lanes.git("checkout", "-qb", "dev", ancestor)
    current = _workflow(promote={"runs-on": "x", "concurrency": LANED})
    current["on"]["push"] = None
    workflow.write_text(yaml.safe_dump(current))
    lanes.git("add", ".")
    lanes.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "dev")
    if shallow:
        clone = tmp_path / "shallow"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", repo.as_uri(), str(clone)], check=True)
        monkeypatch.chdir(clone)
        lanes.git("fetch", "--quiet", "--depth", "1", "origin", head)
        repo = clone
    expected = (
        {f"{lanes.WORKFLOWS}/replay.yml": ["job escape has no shared hosted lane concurrency group"]} if added else {}
    )
    assert lanes.refused(head, repo) == expected


@pytest.mark.parametrize("fault", ["none", "steps", "events", "name", "concurrency", "copy"])
def test_ancestor_trust_requires_the_same_job_and_execution_context(fault):
    ancestor = _workflow(promote={"runs-on": "x", "steps": [{"run": "echo inherited"}]})
    workflow = yaml.safe_load(yaml.safe_dump(ancestor))
    current = _workflow(promote={"runs-on": "x", "concurrency": LANED})
    current["on"]["push"] = None
    if fault == "steps":
        workflow["jobs"]["promote"]["steps"][0]["run"] = "echo changed"
    elif fault == "events":
        workflow["on"]["push"] = None
    elif fault == "name":
        workflow["name"] = "Copy"
    elif fault == "concurrency":
        workflow["concurrency"] = {"group": "branch-only"}
    elif fault == "copy":
        workflow["jobs"]["copy"] = workflow["jobs"].pop("promote")
    expected = (
        []
        if fault == "none"
        else [f"job {'copy' if fault == 'copy' else 'promote'} has no shared hosted lane concurrency group"]
    )
    assert lanes.grade(yaml.safe_dump(workflow), yaml.safe_dump(current), yaml.safe_dump(ancestor)) == expected


@pytest.mark.parametrize("called_block, unsafe", [(CALLED, True), (LANED, False)])
def test_an_edited_trusted_workflow_grades_new_reusable_calls(tmp_path, monkeypatch, called_block, unsafe):
    trusted = {"group": "trusted-${{ github.run_id }}", "cancel-in-progress": False}
    baseline = _workflow(concurrency=trusted, allowed={"runs-on": "x"})
    updated = _workflow(
        concurrency=trusted,
        allowed={"runs-on": "x"},
        escape={"uses": "./.github/workflows/branch.yml"},
    )
    baseline["name"] = updated["name"] = "Trusted"
    child = _workflow(concurrency=called_block)
    child["on"] = {"workflow_call": None, "workflow_dispatch": None}
    base, head, sha = _repo(tmp_path, {"replay.yml": updated, "branch.yml": child})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == (
        {
            f"{lanes.WORKFLOWS}/replay.yml": [
                "job escape calls .github/workflows/branch.yml: job replay has no shared hosted lane concurrency group"
            ]
        }
        if unsafe
        else {}
    )


@pytest.mark.parametrize("block", [LANED, CALLED])
@pytest.mark.parametrize("scope", ["workflow", "job"])
@pytest.mark.parametrize("branch_only", [False, True])
def test_a_hosted_caller_covers_reusable_work_without_its_own_lane(tmp_path, monkeypatch, block, scope, branch_only):
    baseline = _workflow(concurrency=block)
    caller = {"uses": "./.github/workflows/trusted.yml"}
    if scope == "job":
        caller["concurrency"] = block
    updated = _workflow(concurrency=block if scope == "workflow" else None, replay=caller)
    child = (
        _workflow(
            {
                key: value.replace("Replay", "Other") if isinstance(value, str) else value
                for key, value in CALLED.items()
            }
        )
        if branch_only
        else _workflow()
    )
    child["name"] = "Other"
    base, head, sha = _repo(tmp_path, {"replay.yml": updated, "trusted.yml": child})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    if not branch_only:
        (base / lanes.WORKFLOWS / "trusted.yml").write_text(yaml.safe_dump(child))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


@pytest.mark.parametrize("caller_name", ["Trusted", "Replay"])
def test_reusable_walk_follows_trusted_calls_and_preserves_the_outer_workflow_name(tmp_path, monkeypatch, caller_name):
    trusted = {"group": "trusted-${{ github.run_id }}"}
    baseline = _workflow(concurrency=trusted)
    baseline["name"] = caller_name
    updated = {
        **baseline,
        "jobs": {
            "aaa": None,
            "covered": {"uses": "./.github/workflows/branch.yml", "concurrency": LANED},
            "escape": {"uses": "./.github/workflows/middle.yml"},
            "last": {"uses": "./.github/workflows/branch.yml"},
        },
    }
    middle = _workflow(
        concurrency=CALLED,
        cycle={"uses": "./.github/workflows/replay.yml"},
        work={"uses": "./.github/workflows/branch.yml"},
    )
    child = _workflow(concurrency=CALLED)
    base, head, sha = _repo(tmp_path, {"replay.yml": updated, "middle.yml": middle, "branch.yml": child})
    for name, body in {"replay.yml": baseline, "middle.yml": middle}.items():
        (base / lanes.WORKFLOWS / name).write_text(yaml.safe_dump(body))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == (
        {
            f"{lanes.WORKFLOWS}/replay.yml": [
                "job escape calls .github/workflows/middle.yml: job work calls .github/workflows/branch.yml: job replay has no shared hosted lane concurrency group",
                "job last calls .github/workflows/branch.yml: job replay has no shared hosted lane concurrency group",
            ]
        }
        if caller_name == "Trusted"
        else {}
    )


@pytest.mark.parametrize(
    "body, reason",
    [
        ("[", "does not parse as YAML"),
        (None, "is not a workflow mapping"),
        ([], "is not a workflow mapping"),
        ({"jobs": ["invalid"]}, "has no jobs mapping"),
    ],
)
def test_invalid_called_workflows_are_reported_without_hiding_later_calls(tmp_path, monkeypatch, body, reason):
    trusted = {"group": "trusted-${{ github.run_id }}"}
    baseline = _workflow(concurrency=trusted)
    updated = _workflow(
        concurrency=trusted,
        first={"uses": "./.github/workflows/broken.yml"},
        second={"uses": "./.github/workflows/broken.yml"},
    )
    base, head, sha = _repo(tmp_path, {"replay.yml": updated, "broken.yml": body})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/broken.yml": [reason],
        f"{lanes.WORKFLOWS}/replay.yml": [
            f"job first calls .github/workflows/broken.yml: {reason}",
            f"job second calls .github/workflows/broken.yml: {reason}",
        ],
    }


def test_proof_branch_pushes_are_graded():
    workflow = yaml.safe_load((ROOT / lanes.WORKFLOWS / "branch-lanes.yml").read_text())
    assert "diffcheck/**" not in workflow[True]["push"]["branches-ignore"]


def test_a_head_whose_new_workflows_all_queue_on_a_lane_is_allowed(tmp_path, monkeypatch, capsys):
    base, head, sha = _repo(tmp_path, {"laned.yml": _workflow(LANED)})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    monkeypatch.setattr(lanes, "cancel", lambda *a: pytest.fail("nothing to cancel"))
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 0
    assert capsys.readouterr().out == "Every workflow that exists only on this head queues on a shared hosted lane.\n"


def test_a_refused_head_is_red_names_each_reason_and_cancels_its_runs(tmp_path, monkeypatch, capsys):
    base, head, sha = _repo(
        tmp_path, {"plant.yml": {**_workflow(), "on": {"push": None}}, "laned.yml": _workflow(LANED)}
    )
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


@pytest.mark.parametrize(
    "on", [{"release": {"types": ["published"]}}, "workflow_dispatch", ["schedule", "workflow_call"], None]
)
def test_a_refused_workflow_the_push_cannot_trigger_is_red_without_waiting_for_its_run(
    tmp_path, monkeypatch, capsys, on
):
    base, head, sha = _repo(tmp_path, {"release.yml": {**_workflow(), "on": on}})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    monkeypatch.setattr(lanes, "cancel", lambda *a: pytest.fail("no push run to wait for"))
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 1
    assert capsys.readouterr().out == (
        f"Refused {lanes.WORKFLOWS}/release.yml: job replay has no shared hosted lane concurrency group.\n"
    )


@pytest.mark.parametrize("on", ["push", ["release", "pull_request"], {"pull_request_target": None}])
def test_only_refused_workflows_the_push_can_trigger_are_waited_for(tmp_path, monkeypatch, on):
    files = {"fired.yml": {**_workflow(), "on": on}, "release.yml": {**_workflow(), "on": "release"}}
    base, head, sha = _repo(tmp_path, files)
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    calls = []
    monkeypatch.setattr(lanes, "cancel", lambda *args: calls.append(args) or set())
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 1
    assert calls == [("o/r", sha, {f"{lanes.WORKFLOWS}/fired.yml"}, 60.0)]


def test_an_unquoted_on_key_is_read_as_the_push_trigger(tmp_path, monkeypatch):
    body = "name: Replay\non:\n  push:\njobs:\n  replay:\n    runs-on: x\n"
    base, head, sha = _repo(tmp_path, {"fired.yml": body})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    calls = []
    monkeypatch.setattr(lanes, "cancel", lambda *args: calls.append(args) or set())
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 1
    assert calls == [("o/r", sha, {f"{lanes.WORKFLOWS}/fired.yml"}, 60.0)]


@pytest.mark.parametrize("body", ["jobs: [\n", "- push\n"])
def test_a_refused_workflow_that_is_not_a_mapping_is_still_waited_for(tmp_path, monkeypatch, body):
    base, head, sha = _repo(tmp_path, {"broken.yml": body})
    monkeypatch.chdir(base)
    monkeypatch.setenv("GIT_DIR", str(head / ".git"))
    calls = []
    monkeypatch.setattr(lanes, "cancel", lambda *args: calls.append(args) or set())
    assert lanes.main(["--head", sha, "--repo", "o/r"]) == 1
    assert calls == [("o/r", sha, {f"{lanes.WORKFLOWS}/broken.yml"}, 60.0)]


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


def test_cancel_ends_at_once_when_every_matching_run_has_completed(monkeypatch):
    monkeypatch.setattr(
        lanes, "api", lambda *a: {"workflow_runs": [_run(1, "a.yml", "completed"), _run(2, "b.yml@x", "completed")]}
    )
    monkeypatch.setattr(lanes, "stop", lambda *a: pytest.fail("nothing left to cancel"))
    monkeypatch.setattr(lanes.time, "sleep", lambda *a: pytest.fail("nothing left to wait for"))
    assert lanes.cancel("o/r", "abc", {"a.yml", "b.yml"}, 60) == set()


def test_cancel_keeps_waiting_while_a_matching_run_still_runs(monkeypatch):
    pages = [
        [_run(1, "a.yml", "completed"), _run(2, "b.yml")],
        [_run(1, "a.yml", "completed"), _run(2, "b.yml")],
    ]
    monkeypatch.setattr(lanes, "api", lambda *a: {"workflow_runs": pages.pop(0)})
    stops = []
    monkeypatch.setattr(lanes, "stop", lambda repo, run: stops.append(run) or len(stops) > 1)
    sleeps = []
    monkeypatch.setattr(lanes.time, "sleep", sleeps.append)
    assert lanes.cancel("o/r", "abc", {"a.yml", "b.yml"}, 60) == {2}
    assert sleeps == [5.0]
    assert stops == [2, 2]


def test_an_accepted_cancel_ends_the_wait_while_the_run_still_shows_running(monkeypatch):
    monkeypatch.setattr(lanes, "api", lambda *a: {"workflow_runs": [_run(1, "a.yml")]})
    monkeypatch.setattr(lanes, "stop", lambda *a: True)
    monkeypatch.setattr(lanes.time, "sleep", lambda *a: pytest.fail("the cancel was accepted"))
    assert lanes.cancel("o/r", "abc", {"a.yml"}, 60) == {1}


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
    assert workflow[True] in (
        {"push": {"branches-ignore": ["dev", "main", "gh-readonly-queue/**", "diffcheck/**"]}},
        {"push": {"branches-ignore": ["dev", "main", "gh-readonly-queue/**"]}},
    )
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read", "actions": "write"}
    steps = workflow["jobs"]["grade"]["steps"]
    assert steps[0]["with"] == {
        "ref": "dev",
        "fetch-depth": 1,
        "sparse-checkout": (
            "/.github/workflows/\n/scripts/__init__.py\n/scripts/gh_config.py\n/scripts/ci_branch_lanes.py\n"
        ),
        "sparse-checkout-cone-mode": False,
        "persist-credentials": False,
    }
    assert {"name": "Fetch the head as data", "run": 'git fetch --no-tags --depth 1 origin "$GITHUB_SHA"'} in steps
    assert steps[-1]["run"] == 'python -m scripts.ci_branch_lanes --head "$GITHUB_SHA" --repo "$GITHUB_REPOSITORY"'


def test_an_edit_cannot_widen_the_events_of_a_trusted_conditional_lane(tmp_path, monkeypatch):
    block = {"group": "${{ github.event_name == 'push' && github.ref || 'hosted-lane-a' }}", "queue": "max"}
    baseline = _workflow(block)
    baseline["on"] = {"push": {"branches": ["dev"]}}
    updated = _workflow(block)
    updated["on"] = {"push": None}
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/replay.yml": ["job replay has no shared hosted lane concurrency group"]
    }


@pytest.mark.parametrize("quoted_head", [False, True])
def test_equivalent_event_key_spellings_preserve_trusted_concurrency(tmp_path, monkeypatch, quoted_head):
    block = {"group": "${{ github.workflow }}", "queue": "max"}
    baseline = _workflow(block)
    updated = _workflow(block, replay={"runs-on": "x", "steps": [{"run": "echo changed"}]})
    unquoted = baseline if quoted_head else updated
    unquoted[True] = unquoted.pop("on")
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


def test_a_second_event_key_spelling_cannot_widen_a_trusted_conditional_lane(tmp_path, monkeypatch):
    block = {"group": "${{ github.event_name == 'push' && github.ref || 'hosted-lane-a' }}", "queue": "max"}
    baseline = _workflow(block)
    baseline.pop("on")
    baseline[True] = {"push": {"branches": ["dev"]}}
    updated = _workflow(block)
    updated["on"] = {"push": None}
    updated[True] = {"push": {"branches": ["dev"]}}
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/replay.yml": ["job replay has no shared hosted lane concurrency group"]
    }


FILTERED = {"pull_request": {"branches": ["dev"], "paths": ["scripts/**"], "paths-ignore": ["docs/**"]}}


@pytest.mark.parametrize(
    "on",
    [
        {"pull_request": {"branches": ["dev"]}},
        {"pull_request": {"branches": ["dev"], "paths-ignore": ["docs/**"]}},
        {"pull_request": {"branches": ["dev"], "paths": ["scripts/**"]}},
    ],
)
def test_deleting_pull_request_path_filters_keeps_the_base_trusted(tmp_path, monkeypatch, on):
    baseline = _workflow()
    baseline["on"] = FILTERED
    updated = _workflow()
    updated["on"] = on
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"steps": [{"run": "echo changed"}]},
        {"runs-on": "y"},
        {"permissions": {"contents": "write"}},
        {"env": {"MODE": "changed"}},
    ],
)
def test_deleting_path_filters_with_any_other_change_loses_the_base_trust(tmp_path, monkeypatch, change):
    baseline = _workflow()
    baseline["on"] = FILTERED
    updated = _workflow(replay={"runs-on": "x", **change})
    updated["on"] = {"pull_request": {"branches": ["dev"]}}
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/replay.yml": ["job replay has no shared hosted lane concurrency group"]
    }


@pytest.mark.parametrize("change", ["job", "permissions"])
def test_deleting_path_filters_with_a_new_job_or_workflow_key_loses_the_base_trust(tmp_path, monkeypatch, change):
    baseline = _workflow()
    baseline["on"] = FILTERED
    updated = _workflow()
    updated["on"] = {"pull_request": {"branches": ["dev"]}}
    if change == "job":
        updated["jobs"]["extra"] = {"runs-on": "x"}
    else:
        updated["permissions"] = {"contents": "write"}
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    refused = lanes.refused(sha, base)[f"{lanes.WORKFLOWS}/replay.yml"]
    assert "job replay has no shared hosted lane concurrency group" in refused


def test_deleting_the_only_pull_request_filter_keeps_the_base_trusted(tmp_path, monkeypatch):
    jobs = "jobs:\n  replay:\n    runs-on: x\n"
    base, head, sha = _repo(tmp_path, {"replay.yml": f"name: Replay\non:\n  pull_request:\n{jobs}"})
    baseline = f"name: Replay\non:\n  pull_request:\n    paths: [scripts/**]\n{jobs}"
    (base / lanes.WORKFLOWS / "replay.yml").write_text(baseline)
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


@pytest.mark.parametrize(
    ("base_on", "head_on"),
    [
        (FILTERED, {"pull_request": {"branches": ["main"]}}),
        (FILTERED, {"pull_request": {"branches": ["dev"], "types": ["opened"]}}),
        (FILTERED, {"pull_request": {"branches": ["dev"]}, "workflow_dispatch": None}),
        (FILTERED, {"pull_request_target": {"branches": ["dev"]}}),
        ({"push": {"branches": ["dev"], "paths": ["scripts/**"]}}, {"push": {"branches": ["dev"]}}),
        ({"pull_request_target": {"paths": ["scripts/**"]}}, {"pull_request_target": None}),
        ({"pull_request": {"branches": ["dev"]}}, FILTERED),
        ({"pull_request": None}, {"pull_request": {"paths": ["scripts/**"]}}),
        ({"pull_request": {"paths": ["scripts/**"]}}, {"pull_request": {"paths": ["hooks/**"]}}),
        ({"pull_request": {"paths": ["scripts/**"]}}, {"pull_request": {"paths-ignore": ["docs/**"]}}),
    ],
)
def test_any_other_event_change_still_loses_the_base_trust(tmp_path, monkeypatch, base_on, head_on):
    baseline = _workflow()
    baseline["on"] = base_on
    updated = _workflow()
    updated["on"] = head_on
    base, head, sha = _repo(tmp_path, {"replay.yml": updated})
    (base / lanes.WORKFLOWS / "replay.yml").write_text(yaml.safe_dump(baseline))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {
        f"{lanes.WORKFLOWS}/replay.yml": ["job replay has no shared hosted lane concurrency group"]
    }


@pytest.mark.parametrize("change", [None, "group", "queue", "cancel", "extra", "name", "events", "untrusted", "job"])
def test_tests_dispatch_concurrency_keeps_the_hosted_lane(change):
    import copy

    baseline = yaml.safe_load((ROOT / lanes.WORKFLOWS / "test.yml").read_text())
    updated = copy.deepcopy(baseline)
    updated["concurrency"]["group"] = (
        "${{ github.event_name == 'workflow_dispatch' && "
        "format('hosted-lane-{0}', ((endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || "
        "endsWith(github.run_id, '2')) && 'a') || ((endsWith(github.run_id, '3') || "
        "endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b') || 'c') || "
        "format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}"
    )
    block = updated["concurrency"]
    if change == "group":
        block["group"] = block["group"].replace("'workflow_dispatch'", "'push'")
    elif change == "queue":
        block["queue"] = "single"
    elif change == "cancel":
        block["cancel-in-progress"] = True
    elif change == "extra":
        block["extra"] = True
    elif change == "name":
        updated["name"] = "Other"
    elif change == "events":
        updated.setdefault("on", updated.get(True, {}))["schedule"] = [{"cron": "0 * * * *"}]
    elif change == "untrusted":
        baseline = None
    elif change == "job":
        updated["jobs"]["untrusted"] = {"concurrency": {"group": "other"}}
    assert bool(lanes.problems(updated, baseline)) == (change is not None)


@pytest.mark.parametrize(("job", "stage", "index"), [("stats", "stats", "part"), ("mutation-shard", "shard", "shard")])
@pytest.mark.parametrize("change", [None, "runs-on", "queue", "cancel", "stage", "index", "group", "untrusted"])
def test_a_trusted_mutation_leg_may_move_to_a_group_of_its_own_run(job, stage, index, change):
    import copy

    baseline = yaml.safe_load((ROOT / lanes.WORKFLOWS / "mutation-preflight.yml").read_text())
    for name in ("stats", "mutation-shard"):
        baseline["jobs"][name]["concurrency"] = {"group": "${{ 'mutation-lane-a' }}", "queue": "max"}
    updated = copy.deepcopy(baseline)
    block = {"group": f"${{{{ format('mutation-preflight-job-{{0}}-{stage}-{{1}}', github.run_id, matrix.{index}) }}}}"}
    updated["jobs"][job]["concurrency"] = block
    if change == "runs-on":
        updated["jobs"][job]["runs-on"] = "ubuntu-latest"
    elif change == "queue":
        block["queue"] = "max"
    elif change == "cancel":
        block["cancel-in-progress"] = True
    elif change == "stage":
        block["group"] = block["group"].replace(f"-{stage}-", "-other-")
    elif change == "index":
        block["group"] = block["group"].replace(f"matrix.{index}", "matrix.other")
    elif change == "group":
        block["group"] = "${{ github.run_id }}"
    elif change == "untrusted":
        baseline = None
    found = [problem for problem in lanes.problems(updated, baseline) if problem.startswith(f"job {job} ")]
    assert bool(found) == (change is not None)


@pytest.mark.parametrize(
    "change",
    [
        None,
        "group",
        "queue",
        "cancel",
        "name",
        "events",
        "job",
        "untrusted",
        "trusted-name",
        "trusted-events",
        "extra-job",
    ],
)
def test_only_the_trusted_chart_release_may_leave_the_shared_proof_lane(change):
    import copy

    baseline = yaml.safe_load((ROOT / lanes.WORKFLOWS / "chart-release.yml").read_text())
    baseline["concurrency"] = {"group": lanes.GROUP, "queue": "max", "cancel-in-progress": False}
    updated = copy.deepcopy(baseline)
    updated["concurrency"] = {"group": "chart-release", "queue": "max", "cancel-in-progress": False}
    if change == "group":
        updated["concurrency"]["group"] = "chart-release-other"
    elif change == "queue":
        updated["concurrency"]["queue"] = "single"
    elif change == "cancel":
        updated["concurrency"]["cancel-in-progress"] = True
    elif change == "name":
        updated["name"] = "Other release"
    elif change == "events":
        updated[True]["workflow_dispatch"] = None
    elif change == "job":
        updated["jobs"]["publish"]["runs-on"] = "ubuntu-latest"
    elif change == "untrusted":
        baseline = None
    elif change == "trusted-name":
        baseline["name"] = updated["name"] = "Other release"
    elif change == "trusted-events":
        baseline[True] = updated[True] = {"workflow_dispatch": None}
    elif change == "extra-job":
        baseline["jobs"]["other"] = updated["jobs"]["other"] = {"runs-on": "ubuntu-latest"}
    found = lanes.problems(updated, baseline)
    assert bool(found) == (change is not None)


SUPERSEDED_LANE = (
    "format('mutation-lane-{0}', "
    "((endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || endsWith(github.run_id, '2')) && 'a') || "
    "((endsWith(github.run_id, '3') || endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b') || 'c')"
)


def _superseded_block(stage, index):
    return {
        "group": f"${{{{ format('{{0}}-{stage}-{{1}}', github.workflow == 'Mutation preflight' && {SUPERSEDED_LANE} "
        f"|| format('mutation-preflight-job-{{0}}', github.run_id), matrix.{index}) }}}}",
        "queue": "${{ github.workflow == 'Mutation preflight' && 'max' || 'single' }}",
    }


@pytest.mark.parametrize(("job", "stage", "index"), [("stats", "stats", "part"), ("mutation-shard", "shard", "shard")])
@pytest.mark.parametrize("change", [None, "runs-on", "queue", "cancel", "stage", "index", "lane", "untrusted"])
def test_a_branch_behind_dev_keeps_the_superseded_mutation_lane(job, stage, index, change):
    import copy

    baseline = _dev_mutation_preflight()
    updated = copy.deepcopy(baseline)
    block = _superseded_block(stage, index)
    updated["jobs"][job]["concurrency"] = block
    if change == "runs-on":
        updated["jobs"][job]["runs-on"] = "ubuntu-latest"
    elif change == "queue":
        block["queue"] = "max"
    elif change == "cancel":
        block["cancel-in-progress"] = True
    elif change == "stage":
        block["group"] = block["group"].replace(f"-{stage}-", "-other-")
    elif change == "index":
        block["group"] = block["group"].replace(f"matrix.{index}", "matrix.other")
    elif change == "lane":
        block["group"] = block["group"].replace("'c')", "'d')")
    elif change == "untrusted":
        baseline = None
    found = [problem for problem in lanes.problems(updated, baseline) if problem.startswith(f"job {job} ")]
    assert bool(found) == (change is not None)


def _dev_mutation_preflight():
    return yaml.safe_load((ROOT / lanes.WORKFLOWS / "mutation-preflight.yml").read_text())


def _superseded_workflow():
    workflow = _dev_mutation_preflight()
    workflow["jobs"]["stats"]["concurrency"] = _superseded_block("stats", "part")
    workflow["jobs"]["mutation-shard"]["concurrency"] = _superseded_block("shard", "shard")
    return workflow


def test_a_head_carrying_the_superseded_mutation_lanes_passes_dev_grading(tmp_path, monkeypatch):
    base, head, sha = _repo(tmp_path, {"mutation-preflight.yml": _superseded_workflow(), "test.yml": "name: Tests\n"})
    (base / lanes.WORKFLOWS / "mutation-preflight.yml").write_text(yaml.safe_dump(_dev_mutation_preflight()))
    monkeypatch.chdir(head)
    assert lanes.refused(sha, base) == {}


@pytest.mark.parametrize(("job", "stage", "index"), [("stats", "stats", "part"), ("mutation-shard", "shard", "shard")])
def test_the_superseded_mutation_lane_is_refused_on_any_other_job(job, stage, index):
    import copy

    baseline = _dev_mutation_preflight()
    updated = copy.deepcopy(baseline)
    updated["jobs"][f"{job}-2"] = {**updated["jobs"][job], "concurrency": _superseded_block(stage, index)}
    assert lanes.problems(updated, baseline) == [f"job {job}-2 has no shared hosted lane concurrency group"]


@pytest.mark.parametrize("change", ["swap", "events"])
def test_the_superseded_mutation_lanes_are_refused_swapped_or_with_new_events(change):
    updated = _superseded_workflow()
    if change == "swap":
        jobs = updated["jobs"]
        jobs["stats"]["concurrency"], jobs["mutation-shard"]["concurrency"] = (
            jobs["mutation-shard"]["concurrency"],
            jobs["stats"]["concurrency"],
        )
    else:
        updated[True] = {"workflow_dispatch": None}
    found = lanes.problems(updated, _dev_mutation_preflight())
    assert "job stats has no shared hosted lane concurrency group" in found
    assert "job mutation-shard has no shared hosted lane concurrency group" in found


def test_a_branch_only_copy_of_the_mutation_preflight_keeps_no_superseded_lane(tmp_path, monkeypatch):
    base, head, sha = _repo(tmp_path, {"mutation-copy.yml": _superseded_workflow(), "test.yml": "name: Tests\n"})
    monkeypatch.chdir(head)
    found = lanes.refused(sha, base)[f"{lanes.WORKFLOWS}/mutation-copy.yml"]
    assert "job stats has no shared hosted lane concurrency group" in found
    assert "job mutation-shard has no shared hosted lane concurrency group" in found
