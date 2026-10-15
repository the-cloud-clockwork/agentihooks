import json
from pathlib import Path

import pytest

from scripts import ci_runner_classes, ci_runner_trust

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"actors": ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"], "queue_actor": "github-merge-queue[bot]"}


def routed(label, policy=POLICY):
    actors = json.dumps(policy["actors"], separators=(",", ":"))
    queue = policy["queue_actor"]
    trusted = (
        "(github.event_name != 'pull_request_target'"
        " && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
        f" && contains(fromJSON('{actors}'), github.actor)"
        f" && contains(fromJSON('{actors}'), github.triggering_actor)"
        " || (github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev')"
        f" && github.actor == '{queue}' && github.triggering_actor == '{queue}')"
    )
    return "${{ " + trusted + " && '" + label + "' || 'ubuntu-latest' }}"


def queue_routed(route=None, policy=POLICY, label="anton-oss"):
    route = route or f"(github.event_name == 'merge_group' && 'anton-oss-queue' || '{label}')"
    return routed(label, policy).replace(f"&& '{label}' ||", f"&& {route} ||")


def workflow(runner="ubuntu-latest"):
    return {"on": {"pull_request": {}}, "jobs": {"build": {"runs-on": runner, "steps": []}}}


def classes(workflows, table):
    return ci_runner_classes.check(workflows, table, POLICY)


def trust(workflows):
    return ci_runner_trust.check(workflows, POLICY)


@pytest.mark.parametrize("cls", ["light", "standard", "batch"])
def test_a_classed_job_accepts_hosted_and_its_routed_runner(cls):
    label = ci_runner_classes.LABELS[cls]
    for runner in ("ubuntu-latest", routed(label)):
        assert classes({"b.yml": workflow(runner)}, {"b.yml/build": cls}) == []


@pytest.mark.parametrize(
    ("cls", "queue"), [("light", False), ("standard", False), ("batch", False), ("standard", True)]
)
def test_a_classed_job_accepts_a_trusted_queue_rerun_selector(cls, queue):
    label = ci_runner_classes.LABELS[cls]
    runner = (queue_routed(label=label) if queue else routed(label)).replace(
        "github.triggering_actor == 'github-merge-queue[bot]'",
        "(github.triggering_actor == 'github-merge-queue[bot]' || "
        'contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor))',
    )
    assert classes({"b.yml": workflow(runner)}, {"b.yml/build": cls}) == []


def test_a_job_with_no_class_is_red():
    assert classes({"b.yml": workflow()}, {}) == ["b.yml/build: runner class missing"]


def test_reusable_calls_are_graded_through_their_callee():
    calls = {"jobs": {"call": {"uses": "./.github/workflows/x.yml"}}}
    assert classes({"b.yml": calls, "x.yml": workflow()}, {"x.yml/build": "light"}) == []


def test_a_reusable_call_to_a_missing_local_workflow_is_red():
    calls = {"jobs": {"call": {"uses": "./.github/workflows/missing.yml"}}}
    problems = classes({"b.yml": calls}, {})
    assert problems == ["b.yml/call: runner classes cannot resolve local workflow ./.github/workflows/missing.yml"]


def test_a_stale_table_entry_is_red():
    problems = classes({"b.yml": workflow()}, {"b.yml/build": "light", "b.yml/gone": "batch"})
    assert problems == ["b.yml/gone: runner class names no job"]


def test_an_unknown_class_is_red():
    problems = classes({"b.yml": workflow()}, {"b.yml/build": "huge"})
    assert problems == ["b.yml/build: runner class huge is not one of hosted, dind, light, standard, batch"]


@pytest.mark.parametrize(
    "cls,label",
    [("light", "anton-oss"), ("light", "anton-oss-batch"), ("standard", "anton-oss-light"), ("batch", "anton-oss")],
)
def test_a_routed_runner_must_match_the_job_class(cls, label):
    problems = classes({"b.yml": workflow(routed(label))}, {"b.yml/build": cls})
    assert problems == [f"b.yml/build: runs-on must be ubuntu-latest or the routed {cls} runner"]


@pytest.mark.parametrize("runner", ["anton-oss-light", "anton", "ubuntu-24.04", "${{ inputs.runner }}"])
def test_any_other_runner_is_red(runner):
    problems = classes({"b.yml": workflow(runner)}, {"b.yml/build": "light"})
    assert problems == ["b.yml/build: runs-on must be ubuntu-latest or the routed light runner"]


@pytest.mark.parametrize("cls", ["hosted", "dind"])
def test_hosted_and_docker_jobs_cannot_route(cls):
    problems = classes({"b.yml": workflow(routed("anton-oss"))}, {"b.yml/build": cls})
    assert problems == [f"b.yml/build: runs-on must be ubuntu-latest for a {cls} job"]


def test_a_routed_runner_must_carry_the_protected_actor_list():
    other = {
        "actors": ["stranger", "agenticore-app[bot]", "tcc-main-ci[bot]"],
        "queue_actor": "github-merge-queue[bot]",
    }
    runner = routed("anton-oss-light", other)
    assert classes({"b.yml": workflow(runner)}, {"b.yml/build": "light"}) != []
    assert trust({"b.yml": workflow(runner)}) != []


@pytest.mark.parametrize("key", sorted(ci_runner_classes.PINNED_HOSTED))
def test_publish_pages_release_protected_gate_and_reuse_stay_hosted(key):
    file, job = key.split("/")
    workflows = {file: {"jobs": {job: {"runs-on": "ubuntu-latest"}}}}
    assert classes(workflows, {key: "hosted"}) == []
    assert classes(workflows, {key: "light"}) == [f"{key}: runner class must be hosted"]


def test_pinned_hosted_jobs_cover_every_named_release_path():
    assert {
        "publish-pypi.yml/publish",
        "publish-pypi.yml/verify",
        "pages.yml/build",
        "pages.yml/deploy",
        "release.yml/release",
        "gate-protected.yml/grade",
        "runner-trust.yml/actors",
    } <= ci_runner_classes.PINNED_HOSTED


@pytest.mark.parametrize("job", ["reuse", "gate-required"])
@pytest.mark.parametrize(
    "cls,runner", [("hosted", "ubuntu-latest"), ("light", "ubuntu-latest"), ("light", routed("anton-oss-light"))]
)
def test_reuse_and_required_gate_accept_hosted_and_trusted_light(job, cls, runner):
    assert classes({"test.yml": {"jobs": {job: {"runs-on": runner}}}}, {f"test.yml/{job}": cls}) == []


@pytest.mark.parametrize("label", ["anton-oss-light", "anton-oss", "anton-oss-batch"])
def test_runner_trust_accepts_the_routed_runner(label):
    assert trust({"b.yml": workflow(routed(label))}) == []


def test_a_routed_runner_that_can_leave_the_oss_sets_is_red_in_both_gates():
    runner = routed("anton-oss").replace("'ubuntu-latest'", "'anton'")
    assert classes({"b.yml": workflow(runner)}, {"b.yml/build": "standard"}) != []
    assert trust({"b.yml": workflow(runner)}) != []


def test_repository_jobs_all_carry_a_runner_class():
    assert ci_runner_classes.main(["--root", str(ROOT)]) == 0


def test_the_root_defaults_to_the_working_directory(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert ci_runner_classes.main([]) == 0


def test_table_is_sorted_json_kept_under_github(tmp_path, capsys):
    table = json.loads((ROOT / ci_runner_classes.TABLE).read_text())
    assert list(table) == sorted(table)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/a.yml").write_text("jobs:\n  x:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ".github/workflows/b.yaml").write_text("jobs:\n  y:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ".github/workflows/trusted-actors.json").write_text(json.dumps(POLICY))
    (tmp_path / ci_runner_classes.TABLE).write_text(json.dumps({"a.yml/x": "light"}))
    assert ci_runner_classes.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "Runner classes: 2 workflows, 2 jobs, 1 problems" in out
    assert "::error::b.yaml/y: runner class missing" in out


def test_the_actor_policy_comes_from_the_grader_not_the_graded_root(tmp_path, monkeypatch):
    widened = {**POLICY, "actors": [*POLICY["actors"], "intruder"]}
    (tmp_path / ".github/workflows").mkdir(parents=True)
    job = {"jobs": {"x": {"runs-on": routed("anton-oss-light", widened), "steps": []}}}
    (tmp_path / ".github/workflows/a.yml").write_text(json.dumps(job))
    (tmp_path / ".github/workflows/trusted-actors.json").write_text(json.dumps(widened))
    (tmp_path / ci_runner_classes.TABLE).write_text(json.dumps({"a.yml/x": "light"}))
    monkeypatch.chdir(ROOT)
    assert ci_runner_classes.main(["--root", str(tmp_path)]) == 1


def test_a_standard_job_may_send_merge_group_runs_to_the_queue_pool():
    assert classes({"b.yml": workflow(queue_routed())}, {"b.yml/build": "standard"}) == []
    assert trust({"b.yml": workflow(queue_routed())}) == []


@pytest.mark.parametrize("cls", ["light", "batch"])
def test_only_the_standard_class_has_a_queue_pool(cls):
    runner = queue_routed(label=ci_runner_classes.LABELS[cls])
    problems = classes({"b.yml": workflow(runner)}, {"b.yml/build": cls})
    assert problems == [f"b.yml/build: runs-on must be ubuntu-latest or the routed {cls} runner"]
    assert trust({"b.yml": workflow(runner)}) != []


@pytest.mark.parametrize(
    "route",
    [
        "(github.event_name == 'merge_group' && 'anton-oss' || 'anton-oss-queue')",
        "(github.event_name == 'push' && 'anton-oss-queue' || 'anton-oss')",
        "(github.event_name != 'merge_group' && 'anton-oss-queue' || 'anton-oss')",
        "(github.event_name == 'merge_group' && 'anton' || 'anton-oss')",
        "(github.event_name == 'merge_group' && 'anton-oss-queue' || 'ubuntu-latest')",
        "'anton-oss-queue'",
    ],
)
def test_any_other_queue_route_is_red_in_both_gates(route):
    runner = queue_routed(route)
    assert classes({"b.yml": workflow(runner)}, {"b.yml/build": "standard"}) == [
        "b.yml/build: runs-on must be ubuntu-latest or the routed standard runner"
    ]
    assert trust({"b.yml": workflow(runner)}) != []


def test_every_standard_test_job_sends_merge_group_runs_to_the_queue_pool():
    import yaml

    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    table = json.loads((ROOT / ci_runner_classes.TABLE).read_text())
    policy = json.loads((ROOT / ci_runner_classes.ACTORS).read_text())
    standard = sorted(
        key.split("/")[1] for key, cls in table.items() if key.startswith("test.yml/") and cls == "standard"
    )
    assert standard
    assert {name: jobs[name]["runs-on"] for name in standard} == {
        name: (routed("anton-oss", policy) if name == "unit-selected" else queue_routed(policy=policy)).replace(
            f"github.triggering_actor == '{policy['queue_actor']}'",
            f"(github.triggering_actor == '{policy['queue_actor']}' || "
            f"contains(fromJSON('{json.dumps(policy['actors'], separators=(',', ':'))}'), github.triggering_actor))",
        )
        for name in standard
    }
