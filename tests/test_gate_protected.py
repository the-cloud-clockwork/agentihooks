import copy
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from scripts import ci_wiring, gate_protected

_ROOT = Path(__file__).resolve().parents[1]
_TODAY = date(2026, 10, 9)
pytestmark = pytest.mark.unit


def _workflow():
    from scripts.ci_branch_lanes import GROUP

    return {
        "concurrency": {"group": GROUP, "queue": "max", "cancel-in-progress": False},
        True: {"pull_request": {"branches": ["dev"]}, "merge_group": None, "push": {"branches": ["dev"]}},
        "jobs": {
            "unit": {"runs-on": "ubuntu-latest"},
            "lint": {"runs-on": "ubuntu-latest"},
            "wiring": {"runs-on": "ubuntu-latest"},
            "gate": {
                "name": "Gate — Required",
                "needs": ["unit", "lint", "wiring"],
                "if": "${{ always() }}",
                "steps": [{"run": "jq -e 'all(.[]; .result == \"success\")'"}],
            },
        },
    }


def _grade(head, base=None, head_config=None, base_config=None):
    base = base or {"test.yml": _workflow()}
    return gate_protected.grade(base, base_config or {}, head, head_config or {}, _TODAY)


def _entry():
    return {"reason": "why", "owner": "ci-1@swarm", "expires": "2026-12-01"}


def test_an_unchanged_head_is_green():
    assert _grade({"test.yml": _workflow()}) == []


def test_a_head_adding_a_gated_job_is_green():
    head = _workflow()
    head["jobs"]["audit"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate"]["needs"].append("audit")
    assert _grade({"test.yml": head}) == []


def test_a_head_dropping_a_need_is_red():
    head = _workflow()
    head["jobs"]["gate"]["needs"].remove("lint")
    problems = _grade({"test.yml": head})
    assert "The head's Gate — Required drops the base need lint." in problems


def test_a_head_deleting_the_wiring_job_with_its_need_is_red():
    head = _workflow()
    del head["jobs"]["wiring"]
    head["jobs"]["gate"]["needs"].remove("wiring")
    assert "The head's Gate — Required drops the base need wiring." in _grade({"test.yml": head})


@pytest.mark.parametrize(
    "field, value",
    [
        ("steps", [{"run": "true"}]),
        ("if", "${{ false }}"),
        ("continue-on-error", True),
    ],
)
def test_a_head_rewriting_the_aggregator_is_red(field, value):
    head = _workflow()
    head["jobs"]["gate"][field] = value
    problems = _grade({"test.yml": head})
    assert f"The head's Gate — Required changes its {field}; the protected branch grades with its own." in problems


def test_a_head_moving_the_aggregator_is_red():
    head = _workflow()
    head["jobs"]["required"] = head["jobs"].pop("gate")
    assert "The head moves Gate — Required from test.yml/gate to test.yml/required." in _grade({"test.yml": head})


def test_a_head_without_the_aggregator_is_red():
    head = _workflow()
    del head["jobs"]["gate"]
    assert _grade({"test.yml": head}) == ["The head holds 0 jobs named Gate — Required; exactly one must be."]


def test_a_head_adding_a_second_aggregator_is_red():
    head = _workflow()
    extra = {True: {"pull_request": None}, "jobs": {"fake": {"name": "Gate — Required", "steps": [{"run": "true"}]}}}
    assert "The head holds 2 jobs named Gate — Required; exactly one must be." in _grade(
        {"test.yml": head, "extra.yml": extra}
    )


def test_a_head_declaring_its_own_non_gate_entry_is_red():
    head = _workflow()
    head["jobs"]["planted"] = {"runs-on": "ubuntu-latest"}
    problems = _grade({"test.yml": head}, head_config={"not_gates": {"test.yml/planted": _entry()}})
    assert "test.yml/planted runs on pull requests but is not a need of Gate — Required." in problems


def test_a_head_extending_a_base_declaration_keeps_the_base_expiry():
    base = _workflow()
    base["jobs"]["slow"] = {"runs-on": "ubuntu-latest"}
    head = copy.deepcopy(base)
    old = {**_entry(), "expires": "2026-10-01"}
    problems = _grade(
        {"test.yml": head},
        base={"test.yml": base},
        base_config={"not_gates": {"test.yml/slow": old}},
        head_config={"not_gates": {"test.yml/slow": {**old, "expires": "2027-01-01"}}},
    )
    assert "test.yml/slow expired on 2026-10-01." in problems


def test_a_head_retiring_a_declared_job_with_its_entry_is_green():
    base = _workflow()
    base["jobs"]["slow"] = {"runs-on": "ubuntu-latest"}
    problems = _grade(
        {"test.yml": _workflow()},
        base={"test.yml": base},
        base_config={"not_gates": {"test.yml/slow": _entry()}},
    )
    assert problems == []


def test_a_base_without_the_aggregator_is_red():
    base = _workflow()
    del base["jobs"]["gate"]
    assert _grade({"test.yml": _workflow()}, base={"test.yml": base}) == [
        "The base holds 0 jobs named Gate — Required; nothing protected can grade."
    ]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, workflows: dict, config: dict, message: str) -> str:
    folder = repo / ".github/workflows"
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.iterdir():
        path.unlink()
    for name, workflow in workflows.items():
        (folder / name).write_text(yaml.safe_dump(workflow, allow_unicode=True))
    (repo / ".github/gate-wiring.json").write_text(json.dumps(config))
    _git(repo, "add", "-A", ".github")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "dev")
    return tmp_path


def test_main_grades_the_head_against_its_merge_base(repo, capsys):
    base = _commit(repo, {"test.yml": _workflow()}, {}, "base")
    head = _workflow()
    head["jobs"]["gate"]["needs"].remove("lint")
    _git(repo, "checkout", "-q", "-b", "pr")
    planted = _commit(repo, {"test.yml": head}, {}, "drop lint")
    _git(repo, "checkout", "-q", "dev")
    later = _workflow()
    later["jobs"]["late"] = {"runs-on": "ubuntu-latest"}
    later["jobs"]["gate"]["needs"].append("late")
    _commit(repo, {"test.yml": later}, {}, "dev moves on")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"graded {planted[:12]} against merge base {base[:12]}: 2 protected gate problems"
    assert "::error::The head's Gate — Required drops the base need lint." in out
    assert "late" not in out


def test_main_passes_a_clean_head(repo, capsys):
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = _workflow()
    head["jobs"]["audit"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate"]["needs"].append("audit")
    clean = _commit(repo, {"test.yml": head}, {}, "add audit")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", clean]) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


def test_main_reads_declarations_from_the_protected_tip(repo, capsys):
    workflow = _workflow()
    workflow["jobs"]["slow"] = {"runs-on": "ubuntu-latest"}
    expired = {"not_gates": {"test.yml/slow": {**_entry(), "expires": "2000-01-01"}}}
    _commit(repo, {"test.yml": workflow}, expired, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = copy.deepcopy(workflow)
    head["jobs"]["audit"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate"]["needs"].append("audit")
    behind = _commit(repo, {"test.yml": head}, expired, "add audit")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", behind]) == 1
    assert "test.yml/slow expired on 2000-01-01." in capsys.readouterr().out
    _commit(
        repo, {"test.yml": workflow}, {"not_gates": {"test.yml/slow": {**_entry(), "expires": "2999-01-01"}}}, "renew"
    )
    assert gate_protected.main(["--root", str(repo), "--head", behind]) == 0


def test_main_keeps_declarations_dev_retired_after_the_head_branched(repo, capsys):
    workflow = _workflow()
    workflow["jobs"]["slow"] = {"runs-on": "ubuntu-latest"}
    other = {True: {"pull_request": None}, "jobs": {"job": {"runs-on": "ubuntu-latest"}}}
    lasting = {**_entry(), "expires": "2999-01-01"}
    declared = {"outside_gate": {"other.yml/job": lasting}, "not_gates": {"test.yml/slow": lasting}}
    _commit(repo, {"test.yml": workflow, "other.yml": other}, declared, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = copy.deepcopy(workflow)
    head["jobs"]["audit"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate"]["needs"].append("audit")
    behind = _commit(repo, {"test.yml": head, "other.yml": other}, declared, "add audit")
    _git(repo, "checkout", "-q", "dev")
    _commit(repo, {"test.yml": _workflow()}, {}, "retire slow and other")
    assert gate_protected.main(["--root", str(repo), "--head", behind]) == 0
    assert capsys.readouterr().out.splitlines()[-1].endswith(": 0 protected gate problems")


def test_main_reads_top_level_workflows_only_and_runs_without_a_wiring_file(repo, monkeypatch, capsys):
    _commit(repo, {"test.yaml": _workflow()}, {}, "base")
    (repo / ".github/gate-wiring.json").unlink()
    (repo / ".github/workflows/README.md").write_text("not a workflow\n")
    (repo / ".github/workflows/empty.yml").write_text("")
    nested = {True: {"pull_request": None}, "jobs": {"fake": {"name": "Gate — Required"}}}
    (repo / ".github/workflows/old").mkdir()
    (repo / ".github/workflows/old/gate.yml").write_text(yaml.safe_dump(nested, allow_unicode=True))
    _git(repo, "add", "-A", ".github")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "extras")
    monkeypatch.chdir(repo)
    assert gate_protected.main(["--head", "HEAD"]) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


def test_main_needs_a_head():
    with pytest.raises(SystemExit):
        gate_protected.main([])


def test_main_grades_expiry_on_the_utc_date(repo, monkeypatch):
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    zones = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            zones.append(tz)
            return datetime(2026, 10, 9, tzinfo=tz)

    monkeypatch.setattr(gate_protected, "datetime", Clock)
    assert gate_protected.main(["--root", str(repo), "--head", "HEAD"]) == 0
    assert zones == [UTC]


def test_main_refuses_an_unknown_head(repo):
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    with pytest.raises(subprocess.CalledProcessError):
        gate_protected.main(["--root", str(repo), "--head", "0" * 40])


def test_the_repository_grades_itself_green():
    from scripts import ci_runner_trust

    workflows = ci_wiring.load(_ROOT)
    config = json.loads((_ROOT / ci_wiring.CONFIG).read_text())
    actors = json.loads((_ROOT / ci_runner_trust.ACTORS).read_text())
    assert gate_protected.grade(workflows, config, workflows, config, _TODAY, actors) == []


def _protected_workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/gate-protected.yml").read_text())


def test_the_protected_grader_runs_only_from_the_base_on_pull_requests_into_dev():
    workflow = _protected_workflow()
    assert workflow[True] == {
        "pull_request_target": {"branches": ["dev"], "types": ["opened", "synchronize", "reopened"]}
    }
    assert workflow["permissions"] == {"contents": "read", "statuses": "write"}


def test_the_protected_grader_never_executes_the_head():
    steps = _protected_workflow()["jobs"]["grade"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.base_ref }}"
    assert checkout["with"]["persist-credentials"] is False
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "pip install" not in runs.replace('python -m pip install "pyyaml>=6.0"', "")
    assert 'python -m scripts.gate_protected --head "$HEAD_SHA"' in runs
    assert "git checkout" not in runs and "git worktree" not in runs


@pytest.mark.parametrize("name", ["test.yml", "branch-lanes.yml"])
def test_branch_lane_grading_holds_the_required_status_from_the_base(repo, capsys, name):
    from scripts.ci_branch_lanes import GROUP

    base = _workflow()
    base["concurrency"] = {"group": GROUP, "queue": "max", "cancel-in-progress": False}
    _commit(repo, {name: base}, {}, "base lane")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = _workflow()
    head.pop("concurrency")
    planted = _commit(repo, {name: head}, {}, "drop workflow lane")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == 1
    assert "shared hosted lane concurrency group" in capsys.readouterr().out


def test_the_required_grader_preserves_existing_dev_concurrency(repo):
    base = _workflow()
    base["concurrency"] = {"group": "existing-dev-policy"}
    _commit(repo, {"test.yml": base}, {}, "base concurrency")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = _workflow()
    head["concurrency"] = base["concurrency"]
    head["jobs"]["lint"]["steps"] = [{"run": "echo changed"}]
    changed = _commit(repo, {"test.yml": head}, {}, "change lint steps")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", changed]) == 0


@pytest.mark.parametrize("fault", ["skip", "command", "proofs", "delete"])
def test_the_required_grader_refuses_a_disabled_branch_lane_preflight(repo, fault, capsys):
    from copy import deepcopy

    preflight = yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text())
    _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": preflight}, {}, "trusted preflight")
    _git(repo, "checkout", "-q", "-b", "pr")
    altered = deepcopy(preflight)
    if fault == "skip":
        altered["jobs"]["grade"]["if"] = False
    elif fault == "command":
        altered["jobs"]["grade"]["steps"][-1]["run"] = "echo accepted"
    elif fault == "proofs":
        altered[True]["push"]["branches-ignore"].append("diffcheck/**")
    files = {"test.yml": _workflow(), "branch-lanes.yml": altered}
    if fault == "delete":
        files.pop("branch-lanes.yml")
    planted = _commit(repo, files, {}, "disable preflight")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == 1
    out = capsys.readouterr().out
    assert "::error::The head changes or removes the trusted branch lane preflight." in out.splitlines()
    assert ("shared hosted lane concurrency group" in out) is (fault == "proofs")


_SPARSE = "/.github/workflows/\n/scripts/__init__.py\n/scripts/gh_config.py\n/scripts/ci_branch_lanes.py\n"
_SHALLOW_FETCH = 'git fetch --no-tags --depth 1 origin "$GITHUB_SHA"'
_FULL_FETCH = 'git fetch --no-tags origin "$GITHUB_SHA"'


def _fetch_step(preflight):
    return next(step for step in preflight["jobs"]["grade"]["steps"] if step.get("run", "").startswith("git fetch"))


def _shallow_preflight(preflight):
    shallow = copy.deepcopy(preflight)
    checkout = shallow["jobs"]["grade"]["steps"][0]["with"]
    checkout.update({"fetch-depth": 1, "sparse-checkout": _SPARSE, "sparse-checkout-cone-mode": False})
    _fetch_step(shallow)["run"] = _SHALLOW_FETCH
    return shallow


def _full_preflight(preflight):
    full = copy.deepcopy(preflight)
    checkout = full["jobs"]["grade"]["steps"][0]["with"]
    checkout.pop("sparse-checkout", None)
    checkout.pop("sparse-checkout-cone-mode", None)
    checkout["fetch-depth"] = 0
    _fetch_step(full)["run"] = _FULL_FETCH
    return full


@pytest.mark.parametrize(
    "fault", [None, "wider", "cone", "history", "fetch", "proofs", "fetch_full", "no_sparse", "no_depth"]
)
def test_the_required_grader_accepts_only_the_exact_shallow_sparse_preflight(repo, fault, capsys):
    preflight = _full_preflight(yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text()))
    _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": preflight}, {}, "trusted preflight")
    _git(repo, "checkout", "-q", "-b", "pr")
    shallow = _shallow_preflight(preflight)
    checkout = shallow["jobs"]["grade"]["steps"][0]["with"]
    if fault == "wider":
        checkout["sparse-checkout"] += "/scripts/\n"
    elif fault == "cone":
        checkout["sparse-checkout-cone-mode"] = True
    elif fault == "history":
        checkout["fetch-depth"] = 0
    elif fault == "fetch":
        _fetch_step(shallow)["run"] = "git fetch --no-tags origin dev"
    elif fault == "proofs":
        shallow[True]["push"]["branches-ignore"].append("diffcheck/**")
    elif fault == "fetch_full":
        _fetch_step(shallow)["run"] = _FULL_FETCH
    elif fault == "no_sparse":
        checkout.pop("sparse-checkout")
        checkout.pop("sparse-checkout-cone-mode")
    elif fault == "no_depth":
        checkout.pop("fetch-depth")
    planted = _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": shallow}, {}, "shallow preflight")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == (0 if fault is None else 1)
    refused = "::error::The head changes or removes the trusted branch lane preflight." in capsys.readouterr().out
    assert refused is (fault is not None)


@pytest.mark.parametrize("fault", [None, "history", "sparse", "fetch"])
def test_a_branch_still_carrying_the_full_preflight_passes_a_shallow_base(repo, fault):
    preflight = yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text())
    _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": _shallow_preflight(preflight)}, {}, "shallow base")
    _git(repo, "checkout", "-q", "-b", "pr")
    full = _full_preflight(preflight)
    checkout = full["jobs"]["grade"]["steps"][0]["with"]
    if fault == "history":
        checkout["fetch-depth"] = 1
    elif fault == "sparse":
        checkout["sparse-checkout"] = _SPARSE
    elif fault == "fetch":
        _fetch_step(full)["run"] = _SHALLOW_FETCH
    planted = _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": full}, {}, "full preflight")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == (0 if fault is None else 1)


@pytest.mark.parametrize(
    ("form", "cover_proofs"), [(_full_preflight, True), (_shallow_preflight, True), (_shallow_preflight, False)]
)
def test_either_checkout_form_covers_proof_branches_from_a_preflight_that_ignored_them(repo, form, cover_proofs):
    preflight = yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text())
    old = _full_preflight(preflight)
    if "diffcheck/**" not in old[True]["push"]["branches-ignore"]:
        old[True]["push"]["branches-ignore"].append("diffcheck/**")
    _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": old}, {}, "old preflight")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = form(old)
    if cover_proofs:
        head[True]["push"]["branches-ignore"].remove("diffcheck/**")
    planted = _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": head}, {}, "covering preflight")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == 0


@pytest.mark.parametrize("cover_proofs", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
def test_the_required_grader_preserves_preflight_and_allows_proof_coverage(repo, cover_proofs, quoted):
    from copy import deepcopy

    preflight = yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text())
    event_key = "on" if quoted else True
    if quoted:
        preflight["on"] = preflight.pop(True)
    old = deepcopy(preflight)
    old[event_key]["push"]["branches-ignore"].append("diffcheck/**")
    _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": old}, {}, "old preflight")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = preflight if cover_proofs else old
    workflow = _workflow()
    workflow["jobs"]["lint"]["steps"] = [{"run": "echo changed"}]
    changed = _commit(repo, {"test.yml": workflow, "branch-lanes.yml": head}, {}, "retain preflight")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", changed]) == 0


def test_the_protected_grader_reports_gate_required_on_the_head():
    steps = _protected_workflow()["jobs"]["grade"]["steps"]
    statuses = [step for step in steps if "statuses/$HEAD_SHA" in step.get("run", "")]
    assert len(statuses) == 2
    assert all('context="$CONTEXT"' in step["run"] for step in statuses)
    assert _protected_workflow()["env"]["CONTEXT"] == gate_protected.GATE
    assert statuses[-1]["if"] == "${{ always() }}"


@pytest.mark.parametrize("weakened", [False, True])
def test_only_the_canonical_reuse_protocol_is_an_allowed_transition(weakened):
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["gate-required"] = base["jobs"].pop("gate")
    head = copy.deepcopy(base)
    head["jobs"]["reuse"] = ci_reuse.reuse_job()
    head["jobs"]["queue-baseline"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate-required"]["needs"].extend(["reuse", "queue-baseline"])
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps()
    if weakened:
        head["jobs"]["gate-required"]["steps"][0]["run"] += "true\n"
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY)
    assert bool(problems) == weakened, problems


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {},
        {"with": {"ref": "untrusted"}},
        {"with": {"fetch-depth": 1}},
        {"with": {"filter": "blob:none"}},
        {"with": {"repository": "untrusted/repository"}},
        {"env": {"EXTRA": "1"}},
        {"env": {"GIT_HTTP_LOW_SPEED_LIMIT": "999"}},
        {"env": {"GIT_HTTP_LOW_SPEED_TIME": "61"}},
    ],
)
def test_protected_provenance_checkout_accepts_only_exact_git_bounds(bounded, change):
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["reuse"] = {"if": "github.event_name != 'merge_group' && github.event_name != 'pull_request'"}
    base["jobs"]["select"] = {"steps": ci_reuse.duration_provenance_steps()}
    base["jobs"]["gate"]["needs"].append("select")
    head = copy.deepcopy(base)
    checkout = head["jobs"]["select"]["steps"][0]
    if bounded:
        checkout["env"] = {
            "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
            "GIT_HTTP_LOW_SPEED_TIME": "60",
        }
    for field, values in change.items():
        checkout[field] = checkout.get(field, {}) | values
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY)
    assert problems == (["The head skips reuse without protected durations provenance."] if change else [])


@pytest.mark.parametrize("weakened", [False, True])
@pytest.mark.parametrize("anton", [False, True])
def test_the_protected_grader_admits_only_the_exact_paged_reuse_variant(weakened, anton):
    from scripts import ci_reuse

    base, head = _protected_pair()
    head["jobs"]["reuse"] = ci_reuse.paged_reuse_job()
    policy = _POLICY if anton else None
    if anton:
        head["jobs"]["reuse"]["runs-on"] = _anton()
        [install] = [
            step for step in head["jobs"]["reuse"]["steps"] if step.get("name") == "Install metadata dependencies"
        ]
        install["env"] = {"PYTHONNOUSERSITE": "1"}
    if weakened:
        [step] = [
            step
            for step in head["jobs"]["reuse"]["steps"]
            if step.get("name") == "Fetch the evidence of recent passed runs"
        ]
        step["run"] = step["run"].replace("--page-size 5", "--page-size 100")
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY, policy)
    assert bool(problems) == weakened, problems


def test_a_head_cannot_replace_the_protected_reuse_decision():
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["gate-required"] = base["jobs"].pop("gate")
    base["jobs"]["reuse"] = ci_reuse.reuse_job()
    base["jobs"]["gate-required"]["needs"].append("reuse")
    head = copy.deepcopy(base)
    head["jobs"]["reuse"]["steps"] = [{"run": "echo reused=true"}]
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY)
    assert "The head changes the protected reuse job; the protected branch grades with its own." in problems


def test_an_unchanged_protected_reuse_producer_stays_accepted():
    base = _workflow()
    base["jobs"]["reuse"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "echo legacy"}]}
    base["jobs"]["gate"]["needs"].append("reuse")
    assert gate_protected.grade({"test.yml": base}, {}, {"test.yml": copy.deepcopy(base)}, {}, _TODAY) == []


def test_removing_the_protected_reuse_producer_names_the_violation():
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["reuse"] = ci_reuse.reuse_job()
    base["jobs"]["gate"]["needs"].append("reuse")
    head = copy.deepcopy(base)
    del head["jobs"]["reuse"]
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY)
    assert "The head removes the protected reuse job." in problems


def test_a_head_moving_the_gate_away_from_the_protected_reuse_job_is_named_not_crashed():
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["reuse"] = ci_reuse.reuse_job()
    base["jobs"]["gate"]["needs"].append("reuse")
    problems = gate_protected.grade({"test.yml": base}, {}, {"other.yml": copy.deepcopy(base)}, {}, _TODAY)
    assert "The head removes the protected reuse job." in problems


_POLICY = {
    "actors": ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"],
    "queue_actor": "github-merge-queue[bot]",
}
_MEMBERS = '["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]'


def _anton(pool="anton-oss-light"):
    return (
        "${{ (github.event_name != 'pull_request_target' && (github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository) && "
        f"contains(fromJSON('{_MEMBERS}'), github.actor) && contains(fromJSON('{_MEMBERS}'), github.triggering_actor) || "
        "(github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev') && "
        f"github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]') && '{pool}' || 'ubuntu-latest' }}}}"
    )


def _anton_reuse(pool="anton-oss-light"):
    from scripts import ci_reuse

    job = ci_reuse.reuse_job()
    job["runs-on"] = _anton(pool)
    [install] = [step for step in job["steps"] if step.get("name") == "Install metadata dependencies"]
    install["env"] = {"PYTHONNOUSERSITE": "1"}
    return job


def _protected_pair():
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["gate-required"] = base["jobs"].pop("gate")
    base["jobs"]["gate-required"]["runs-on"] = "ubuntu-latest"
    base["jobs"]["reuse"] = ci_reuse.reuse_job()
    base["jobs"]["gate-required"]["needs"].append("reuse")
    return base, copy.deepcopy(base)


def test_the_reuse_job_and_the_gate_may_move_behind_the_runner_trust_selector():
    base, head = _protected_pair()
    head["jobs"]["reuse"] = _anton_reuse()
    head["jobs"]["gate-required"]["runs-on"] = _anton()
    assert gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY, _POLICY) == []
    assert gate_protected.grade({"test.yml": head}, {}, {"test.yml": base}, {}, _TODAY, _POLICY) == []


def test_the_reuse_job_and_gate_accept_trusted_queue_rerun_selectors():
    base, head = _protected_pair()
    runner = _anton().replace(
        "github.triggering_actor == 'github-merge-queue[bot]'",
        "(github.triggering_actor == 'github-merge-queue[bot]' || "
        f"contains(fromJSON('{_MEMBERS}'), github.triggering_actor))",
    )
    head["jobs"]["reuse"] = _anton_reuse()
    head["jobs"]["reuse"]["runs-on"] = runner
    head["jobs"]["gate-required"]["runs-on"] = runner
    assert gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY, _POLICY) == []
    assert gate_protected.grade({"test.yml": head}, {}, {"test.yml": base}, {}, _TODAY, _POLICY) == []


@pytest.mark.parametrize(
    "runner", [_anton("anton-oss"), "anton-oss-light", "ubuntu-22.04", _anton().replace("&&", "||", 1)]
)
def test_the_gate_takes_no_other_runner(runner):
    base, head = _protected_pair()
    head["jobs"]["gate-required"]["runs-on"] = runner
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY, _POLICY)
    assert problems == ["The head's Gate — Required changes its runs-on; the protected branch grades with its own."]


def _without_install_env(job):
    [install] = [step for step in job["steps"] if step.get("name") == "Install metadata dependencies"]
    del install["env"]
    return job


def _changed_step(job):
    job["steps"][-1]["name"] = "renamed"
    return job


def _other_pool(job):
    job["runs-on"] = _anton("anton-oss-batch")
    return job


@pytest.mark.parametrize("weaken", [_without_install_env, _changed_step, _other_pool])
def test_the_reuse_job_moves_only_in_its_canonical_anton_form(weaken):
    base, head = _protected_pair()
    head["jobs"]["reuse"] = weaken(_anton_reuse())
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY, _POLICY)
    assert problems == ["The head changes the protected reuse job; the protected branch grades with its own."]


def test_without_a_dict_policy_only_the_hosted_runner_is_accepted():
    base, head = _protected_pair()
    head["jobs"]["reuse"] = _anton_reuse()
    head["jobs"]["gate-required"]["runs-on"] = _anton()
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY, list(_POLICY["actors"]))
    assert problems == [
        "The head's Gate — Required changes its runs-on; the protected branch grades with its own.",
        "The head changes the protected reuse job; the protected branch grades with its own.",
    ]
    assert gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY) == problems


def _trusted_base(repo, actors=None):
    candidate = _workflow()
    candidate["jobs"]["gate"]["runs-on"] = "ubuntu-latest"
    _commit(repo, {"test.yml": candidate}, {}, "base workflows")
    policy = repo / ".github/workflows/trusted-actors.json"
    policy.write_text(json.dumps(actors or ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"]))
    _git(repo, "add", ".github/workflows/trusted-actors.json")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "protected approvals")
    return candidate, _git(repo, "rev-parse", "HEAD")


def test_protected_cli_accepts_a_hosted_head_from_an_actor_owned_base(repo):
    candidate, base = _trusted_base(repo)
    head = _commit(repo, {"test.yml": candidate}, {}, "hosted head")
    assert gate_protected.main(["--root", str(repo), "--base", base, "--head", head]) == 0


def test_protected_cli_accepts_the_gate_behind_the_selector_of_the_base_policy(repo):
    candidate, base = _trusted_base(repo, _POLICY)
    candidate["jobs"]["gate"]["runs-on"] = _anton()
    head = _commit(repo, {"test.yml": candidate}, {}, "gate on anton")
    assert gate_protected.main(["--root", str(repo), "--base", base, "--head", head]) == 0


def test_protected_cli_refuses_a_policy_without_its_queue_actor_before_building_a_selector(repo):
    candidate, base = _trusted_base(repo, {"actors": _POLICY["actors"]})
    candidate["jobs"]["gate"]["runs-on"] = _anton()
    head = _commit(repo, {"test.yml": candidate}, {}, "gate on anton")
    with pytest.raises(ValueError, match="trusted actors"):
        gate_protected.main(["--root", str(repo), "--base", base, "--head", head])


def test_protected_cli_rejects_a_private_runner_with_no_actor_check(repo, capsys):
    candidate, base = _trusted_base(repo)
    candidate["jobs"]["private"] = {"runs-on": "anton"}
    candidate["jobs"]["gate"]["needs"].append("private")
    head = _commit(repo, {"test.yml": candidate}, {}, "unguarded private head")
    assert gate_protected.main(["--root", str(repo), "--base", base, "--head", head]) == 1
    assert "test.yml/private: runner trust" in capsys.readouterr().out


def test_protected_cli_keeps_wiring_errors_when_runner_admission_is_safe(repo):
    candidate, base = _trusted_base(repo)
    candidate["jobs"]["gate"]["needs"].remove("unit")
    head = _commit(repo, {"test.yml": candidate}, {}, "lost required unit")
    assert gate_protected.main(["--root", str(repo), "--base", base, "--head", head]) == 1


def test_protected_cli_validates_approvals_from_the_base_not_head(repo):
    candidate, base = _trusted_base(repo, ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]", "stranger"])
    head = _commit(repo, {"test.yml": candidate}, {}, "head without policy")
    with pytest.raises(ValueError, match="trusted actors"):
        gate_protected.main(["--root", str(repo), "--base", base, "--head", head])


def _aggregators():
    base, head = _workflow(), _workflow()
    for workflow in (base, head):
        workflow["jobs"]["gate-required"] = workflow["jobs"].pop("gate")
    return base, head


@pytest.mark.parametrize("selected", [False, True])
def test_a_head_switching_to_either_canonical_aggregation_is_green(selected):
    from scripts import ci_reuse

    base, head = _aggregators()
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(selected=selected)
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []


@pytest.mark.parametrize("docs", [False, True])
def test_documentation_aggregation_accepts_only_the_canonical_transition(docs):
    from scripts import ci_reuse

    base, head = _aggregators()
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True, consolidated=True, docs=docs)
    if docs:
        head["jobs"]["docs"] = {"runs-on": "ubuntu-latest"}
        head["jobs"]["gate-required"]["needs"].append("docs")
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []
    head["jobs"]["gate-required"]["steps"][0]["run"] += "\necho weakened"
    assert _grade({"test.yml": head}, base={"test.yml": base})


@pytest.mark.parametrize("postgres", [False, True])
def test_postgres_aggregation_accepts_only_the_canonical_transition(postgres):
    from scripts import ci_reuse

    base, head = _aggregators()
    steps = ci_reuse.gate_steps(queue_tier=True, consolidated=True, docs=True)
    head["jobs"]["gate-required"]["steps"] = ci_reuse.postgres_required(steps) if postgres else steps
    head["jobs"]["docs"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate-required"]["needs"].append("docs")
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []
    head["jobs"]["gate-required"]["steps"][0]["run"] = head["jobs"]["gate-required"]["steps"][0]["run"].replace(
        '"sonar"', '"semgrep"'
    )
    assert _grade({"test.yml": head}, base={"test.yml": base})


def test_a_base_requiring_the_postgres_scenario_refuses_a_head_that_drops_it():
    from scripts import ci_reuse

    base, head = _aggregators()
    steps = ci_reuse.gate_steps(queue_tier=True, consolidated=True)
    base["jobs"]["gate-required"]["steps"] = ci_reuse.postgres_required(steps)
    head["jobs"]["gate-required"]["steps"] = ci_reuse.postgres_required(steps)
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []
    head["jobs"]["gate-required"]["steps"] = steps
    problems = _grade({"test.yml": head}, base={"test.yml": base})
    assert any("changes its steps" in problem for problem in problems), problems


def test_the_tests_gate_may_start_requiring_the_postgres_scenario_on_pull_requests():
    from scripts import ci_reuse

    base = {path.name: yaml.safe_load(path.read_text()) for path in (_ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    gate = head["test.yml"]["jobs"]["gate-required"]
    gate["steps"] = ci_reuse.postgres_required(gate["steps"])
    assert '"sonar", "writer-lock"]' in gate["steps"][0]["run"]
    config = json.loads((_ROOT / ".github/gate-wiring.json").read_text())
    policy = json.loads((_ROOT / ".github/workflows/trusted-actors.json").read_text())
    assert gate_protected.grade(base, config, head, config, _TODAY, policy) == []


@pytest.mark.parametrize("existing", [False, True])
def test_a_declared_documentation_check_cannot_switch_to_legacy_aggregation(existing):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True, consolidated=True)
    head["jobs"]["docs"] = {"runs-on": "ubuntu-latest", "if": False}
    head["jobs"]["gate-required"]["needs"].append("docs")
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True, consolidated=True)
    if existing:
        base["jobs"]["docs"] = {"runs-on": "ubuntu-latest"}
        base["jobs"]["gate-required"]["needs"].append("docs")
        base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True, consolidated=True, docs=True)
    problems = _grade({"test.yml": head}, base={"test.yml": base})
    assert any("changes its steps" in problem for problem in problems), problems


def test_unchanged_noncanonical_documentation_gate_preserves_its_protected_steps():
    base, head = _workflow(), _workflow()
    for workflow in (base, head):
        workflow["jobs"]["docs"] = {"runs-on": "ubuntu-latest"}
        workflow["jobs"]["gate"]["needs"].append("docs")
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []


@pytest.mark.parametrize("change", ["outcome", "byte", "extra"])
@pytest.mark.parametrize("selected", [False, True])
def test_a_head_altering_the_queue_canonical_aggregation_is_red(change, selected):
    from scripts import ci_reuse

    base, head = _aggregators()
    steps = ci_reuse.gate_steps(selected=selected)
    [step] = steps
    altered = {
        "outcome": [{**step, "run": step["run"].replace("success", "failure")}],
        "byte": [{**step, "run": step["run"][:-1]}],
        "extra": [*steps, {"run": "true"}],
    }
    head["jobs"]["gate-required"]["steps"] = altered[change]
    assert _grade({"test.yml": head}, base={"test.yml": base}) == [
        "The head's Gate — Required changes its steps; the protected branch grades with its own."
    ]


_REVIEWED_SUMMARY = """
- uses: actions/checkout@v4
  if: ${{ always() }}
  with:
    ref: ${{ github.sha }}
    sparse-checkout: scripts/ci_gate_summary.py
    sparse-checkout-cone-mode: false
    persist-credentials: false

- name: Download this run's test durations
  if: ${{ always() }}
  uses: actions/download-artifact@v4
  with:
    pattern: durations-3.*
    path: test-reports

- name: Download this run's test flakes
  if: ${{ always() }}
  uses: actions/download-artifact@v4
  with:
    pattern: flakes-3.*
    path: test-reports

- name: Summarize this run's tests
  if: ${{ always() }}
  run: python3 "$GITHUB_WORKSPACE/scripts/ci_gate_summary.py" test-reports | tee -a "$GITHUB_STEP_SUMMARY"
"""

_FORMS = [
    {},
    {"selected": True},
    {"queue_tier": True},
    {"queue_tier": True, "promotion": True},
    {"queue_tier": True, "consolidated": True},
    {"queue_tier": True, "promotion": True, "consolidated": True},
]


@pytest.mark.parametrize("form", _FORMS)
def test_the_summary_follows_the_unchanged_aggregation(form):
    from scripts import ci_reuse

    [aggregation, *summary] = ci_reuse.gate_steps(**form, summary=True)
    assert [aggregation] == ci_reuse.gate_steps(**form)
    assert summary == yaml.safe_load(_REVIEWED_SUMMARY)


@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("summary", [False, True])
def test_a_head_adding_the_reviewed_summary_is_green(form, summary):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(**form)
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(**form, summary=summary)
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []


@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("summary", [False, True])
def test_a_head_taking_the_inline_mutation_form_is_green(form, summary):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(**form)
    head["jobs"]["gate-required"]["steps"] = ci_reuse.inline_mutation(ci_reuse.gate_steps(**form, summary=summary))
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []


@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize(("shell", "problems"), [("bash", 0), ("sh", 1), ("pwsh", 1)])
def test_a_head_running_the_summary_under_pipefail_bash_is_green(form, shell, problems):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(**form, summary=True)
    steps = ci_reuse.gate_steps(**form, summary=True)
    steps[-1]["shell"] = shell
    head["jobs"]["gate-required"]["steps"] = steps
    assert len(_grade({"test.yml": head}, base={"test.yml": base})) == problems
    assert ci_reuse.pipefail_summary(ci_reuse.gate_steps(**form, summary=True))[-1]["shell"] == "bash"
    assert ci_reuse.pipefail_summary(steps)[:-1] == ci_reuse.gate_steps(**form, summary=True)[:-1]
    assert "shell" not in ci_reuse.gate_steps(**form, summary=True)[-1]


_SELECTION_DIAGNOSTIC = """selection=$(jq -r '.select.result // ""' <<< "$NEEDS")
if [[ "$selection" == failure || "$selection" == cancelled ]]; then
  echo "::error::Test selection $selection; fix selection before downstream test and coverage checks."
  exit 1
fi
"""


@pytest.mark.parametrize("diagnostic", [False, True])
def test_selection_diagnostic_preserves_the_current_required_gate(diagnostic):
    base = ci_wiring.load(_ROOT)
    head = copy.deepcopy(base)
    if diagnostic:
        step = head["test.yml"]["jobs"]["gate-required"]["steps"][0]
        step["run"] = _SELECTION_DIAGNOSTIC + step["run"]
    config = json.loads((_ROOT / ci_wiring.CONFIG).read_text())
    actors = json.loads((_ROOT / ".github/workflows/trusted-actors.json").read_text())
    assert gate_protected.grade(base, config, head, config, _TODAY, actors) == []


@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('then .value.result == "success"', 'then .value.result != "failure"'),
        ('"semgrep", "sonar"', '"semgrep"'),
        ("exit 1", "exit 0"),
    ],
)
def test_selection_diagnostic_cannot_weaken_required_success(diagnostic, before, after):
    base = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    head = copy.deepcopy(base)
    step = head["jobs"]["gate-required"]["steps"][0]
    assert before in step["run"]
    step["run"] = step["run"].replace(before, after)
    if diagnostic:
        step["run"] = _SELECTION_DIAGNOSTIC + step["run"]
    assert "The head's Gate — Required changes its steps; the protected branch grades with its own." in _grade(
        {"test.yml": head}, base={"test.yml": base}
    )


def test_selection_diagnostic_transform_only_prepends_the_reviewed_script():
    from scripts import ci_reuse

    steps = ci_reuse.gate_steps(queue_tier=True, promotion=True, consolidated=True, summary=True)
    original = copy.deepcopy(steps)
    transformed = ci_reuse.selection_diagnostic(steps)
    assert transformed == [{**original[0], "run": _SELECTION_DIAGNOSTIC + original[0]["run"]}, *original[1:]]
    assert steps == original


@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("form", _FORMS)
def test_selection_diagnostic_accepts_legacy_canonical_forms(diagnostic, form):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(**form)
    steps = ci_reuse.gate_steps(**form)
    if diagnostic:
        steps[0]["run"] = _SELECTION_DIAGNOSTIC + steps[0]["run"]
    head["jobs"]["gate-required"]["steps"] = steps
    assert _grade({"test.yml": head}, base={"test.yml": base}) == []


@pytest.mark.parametrize("weaken", ["trusted output", "no skip guard"])
def test_a_head_weakening_the_inline_mutation_form_is_red(weaken):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(queue_tier=True, promotion=True)
    [step] = ci_reuse.inline_mutation(ci_reuse.gate_steps(queue_tier=True, promotion=True))
    if weaken == "trusted output":
        step["env"]["INLINE"] = "${{ needs.mutation-plan.outputs.inline }}"
    else:
        step["run"] = step["run"].replace(' and $inline == "true")', ")")
    head["jobs"]["gate-required"]["steps"] = [step]
    assert _grade({"test.yml": head}, base={"test.yml": base}) == [
        "The head's Gate — Required changes its steps; the protected branch grades with its own."
    ]


@pytest.mark.parametrize("change", ["extra", "before", "reordered", "altered", "partial"])
def test_a_head_changing_the_summary_steps_is_red(change):
    from scripts import ci_reuse

    base, head = _aggregators()
    [aggregation, *summary] = ci_reuse.gate_steps(queue_tier=True, promotion=True, summary=True)
    altered = {
        "extra": [aggregation, *summary, {"run": "true"}],
        "before": [{"run": "true"}, aggregation, *summary],
        "reordered": [*summary, aggregation],
        "altered": [aggregation, *summary[:-1], {**summary[-1], "run": "true"}],
        "partial": [aggregation, *summary[1:]],
    }
    head["jobs"]["gate-required"]["steps"] = altered[change]
    assert _grade({"test.yml": head}, base={"test.yml": base}) == [
        "The head's Gate — Required changes its steps; the protected branch grades with its own."
    ]


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("runner", ["ubuntu-latest", _anton()])
def test_the_reviewed_summary_may_run_outside_required_grading(separate, declared, runner):
    from scripts import ci_reuse

    base, head = _aggregators()
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(summary=True)
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(summary=not separate)
    config = {}
    if separate:
        head["jobs"]["test-summary"] = {
            "needs": ["unit", "unit-selected"],
            "if": "${{ always() }}",
            "runs-on": runner,
            "timeout-minutes": 5,
            "steps": yaml.safe_load(_REVIEWED_SUMMARY),
        }
        head["jobs"]["unit-selected"] = {"runs-on": "ubuntu-latest"}
        head["jobs"]["gate-required"]["needs"].append("unit-selected")
        if declared:
            config = {"not_gates": {"test.yml/test-summary": _entry()}}
    expected = (
        []
        if not separate or declared
        else ["test.yml/test-summary runs on pull requests but is not a need of Gate — Required."]
    )
    assert gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, config, _TODAY, _POLICY) == expected


@pytest.mark.parametrize("shell", [None, "bash", "sh", "pwsh"])
@pytest.mark.parametrize("declared_on_base", [False, True])
def test_the_separate_summary_accepts_only_the_reviewed_shell(shell, declared_on_base):
    from scripts import ci_reuse

    base, head = _aggregators()
    steps = ci_reuse.summary_steps()
    if shell is not None:
        steps[-1]["shell"] = shell
    head["jobs"]["test-summary"] = {
        "needs": ["unit", "unit-selected"],
        "if": "${{ always() }}",
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 5,
        "steps": steps,
    }
    head["jobs"]["unit-selected"] = {"runs-on": "ubuntu-latest"}
    head["jobs"]["gate-required"]["needs"].append("unit-selected")
    config = {"not_gates": {"test.yml/test-summary": _entry()}}
    problems = gate_protected.grade(
        {"test.yml": base},
        config if declared_on_base else {},
        {"test.yml": head},
        config,
        _TODAY,
        _POLICY,
    )
    if shell in (None, "bash"):
        assert problems == []
    else:
        assert "The test summary must use the reviewed reporting job." in problems


@pytest.mark.parametrize("change", ["steps", "needs", "if", "runs-on", "timeout-minutes", "env"])
@pytest.mark.parametrize("declared_on_base", [False, True])
def test_a_separate_summary_cannot_change_its_reviewed_job(change, declared_on_base):
    from scripts import ci_reuse

    base, head = _aggregators()
    summary = {
        "needs": ["unit", "unit-selected"],
        "if": "${{ always() }}",
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 5,
        "steps": ci_reuse.summary_steps(),
    }
    summary[change] = {
        "steps": [{"run": "true"}],
        "needs": ["unit"],
        "if": "${{ success() }}",
        "runs-on": "anton",
        "timeout-minutes": 30,
        "env": {"EXTRA": "value"},
    }[change]
    head["jobs"]["test-summary"] = summary
    config = {"not_gates": {"test.yml/test-summary": _entry()}}
    problems = _grade(
        {"test.yml": head},
        base={"test.yml": base},
        head_config=config,
        base_config=config if declared_on_base else {},
    )
    assert "The test summary must use the reviewed reporting job." in problems


def test_the_separate_summary_keeps_the_protected_declaration_expiry():
    from scripts import ci_reuse

    base, head = _aggregators()
    head["jobs"]["test-summary"] = {
        "needs": ["unit", "unit-selected"],
        "if": "${{ always() }}",
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 5,
        "steps": ci_reuse.summary_steps(),
    }
    key = "test.yml/test-summary"
    problems = _grade(
        {"test.yml": head},
        base={"test.yml": base},
        base_config={"not_gates": {key: {**_entry(), "expires": "2026-10-01"}}},
        head_config={"not_gates": {key: _entry()}},
    )
    assert "test.yml/test-summary expired on 2026-10-01." in problems


@pytest.mark.parametrize("promotion", [False, True])
@pytest.mark.parametrize("weakened", [False, True])
@pytest.mark.parametrize("watcher_loss", [False, True])
@pytest.mark.parametrize("lost_rerun", [False, True])
def test_the_promotion_gate_form_is_an_allowed_transition(weakened, promotion, watcher_loss, lost_rerun):
    from scripts import ci_reuse

    base = _workflow()
    base["jobs"]["gate-required"] = base["jobs"].pop("gate")
    base["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps()
    head = copy.deepcopy(base)
    head["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(
        queue_tier=True, promotion=promotion, watcher_loss=watcher_loss
    )
    if lost_rerun:
        head["jobs"]["gate-required"]["steps"] = ci_reuse.lost_rerun(head["jobs"]["gate-required"]["steps"])
    if weakened:
        head["jobs"]["gate-required"]["steps"][0]["run"] += "true\n"
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {}, _TODAY)
    assert bool(problems) == weakened, problems


@pytest.mark.parametrize(
    ("disabled", "removed", "red"),
    [(True, True, False), (True, False, True), (False, True, True)],
)
def test_only_a_disabled_job_the_head_removes_may_leave_the_gate(disabled, removed, red):
    base = _workflow()
    base["jobs"]["queue-baseline"] = {"runs-on": "ubuntu-latest", "if": False if disabled else "${{ always() }}"}
    base["jobs"]["gate"]["needs"].append("queue-baseline")
    head = copy.deepcopy(base)
    head["jobs"]["gate"]["needs"].remove("queue-baseline")
    if removed:
        del head["jobs"]["queue-baseline"]
    problems = _grade({"test.yml": head}, {"test.yml": base})
    dropped = "The head's Gate — Required drops the base need queue-baseline."
    assert (dropped in problems) == red, problems


@pytest.mark.parametrize(
    ("event", "skipped", "reused", "green"),
    [
        ("push", {"lint", "semgrep", "test-count"}, "false", True),
        ("push", {"lint", "semgrep", "test-count", "queue-baseline"}, "false", True),
        ("push", {"lint", "semgrep", "test-count", "sonar"}, "false", False),
        ("push", {"unit"}, "false", False),
        ("workflow_dispatch", {"lint"}, "false", False),
        ("push", set(), "true", False),
        ("push", set(), "false", True),
    ],
)
def test_the_promotion_gate_skips_only_pull_request_tier_jobs_on_a_push(event, skipped, reused, green):
    from scripts import ci_reuse

    [step] = ci_reuse.gate_steps(queue_tier=True, promotion=True)
    jobs = [
        "durations",
        "split",
        "unit",
        "shard-check",
        "test-count",
        "lint",
        "semgrep",
        "sonar",
        "reuse",
        "queue-baseline",
    ]
    needs = {job: {"result": "skipped" if job in skipped else "success"} for job in jobs}
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps(needs), REUSED=reused, EVENT=event, MUTATION="false", KIND="false"),
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == green, result.stdout + result.stderr


def test_the_required_grader_refuses_widened_events_behind_a_conditional_lane(repo, capsys):
    block = {"group": "${{ github.event_name == 'push' && github.ref || 'hosted-lane-a' }}", "queue": "max"}
    base = {
        "name": "Replay",
        True: {"push": {"branches": ["dev"]}},
        "concurrency": block,
        "jobs": {"replay": {"runs-on": "x"}},
    }
    _commit(repo, {"test.yml": _workflow(), "replay.yml": base}, {}, "conditional lane")
    _git(repo, "checkout", "-q", "-b", "pr")
    widened = {**base, True: {"push": None}}
    planted = _commit(repo, {"test.yml": _workflow(), "replay.yml": widened}, {}, "widen push events")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", planted]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "::error::replay.yml: job replay has no shared hosted lane concurrency group" in out


def test_the_required_grader_skips_lane_grading_of_an_unchanged_workflow(repo, capsys):
    unchanged = {"name": "Replay", "concurrency": float("nan"), "jobs": {"replay": {"runs-on": "x"}}}
    head = _commit(repo, {"test.yml": _workflow(), "replay.yml": unchanged}, {}, "unchanged lane")
    assert gate_protected.main(["--root", str(repo), "--head", head]) == 0
    assert "replay.yml" not in capsys.readouterr().out


@pytest.mark.parametrize("trusted", [False, True])
def test_only_the_trusted_preflight_skips_lane_grading(repo, capsys, trusted):
    preflight = yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text())
    _commit(repo, {"test.yml": _workflow()}, {}, "no preflight")
    _git(repo, "checkout", "-q", "-b", "pr")
    added = _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": preflight}, {}, "add preflight")
    _git(repo, "checkout", "-q", "dev")
    if trusted:
        _commit(repo, {"test.yml": _workflow(), "branch-lanes.yml": preflight}, {}, "trust preflight")
    assert gate_protected.main(["--root", str(repo), "--head", added]) == (0 if trusted else 1)
    assert ("shared hosted lane concurrency group" in capsys.readouterr().out) is not trusted
