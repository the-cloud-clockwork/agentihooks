import json
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

TRUSTED = (
    "(github.event_name != 'pull_request_target'"
    " && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor)'
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor)'
    " || (github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev')"
    " && github.actor == 'github-merge-queue[bot]' && (github.triggering_actor == 'github-merge-queue[bot]'"
    ' || contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor)))'
)


LIGHT, STANDARD, BATCH, QUEUE = "anton-oss-light", "anton-oss", "anton-oss-batch", "anton-oss-queue"
QUEUED = "(github.event_name == 'merge_group' && 'anton-oss-queue' || 'anton-oss')"


def pool(label):
    route = label if label == QUEUED else f"'{label}'"
    return "${{ " + TRUSTED + " && " + route + " || 'ubuntu-latest' }}"


MOVED = {
    ("changelog.yml", "changelog"): LIGHT,
    ("chaos.yml", "chaos"): STANDARD,
    ("chart-release.yml", "publish"): LIGHT,
    ("mutation-line-selection-proof.yml", "compare"): BATCH,
    ("test.yml", "docs"): QUEUED,
    ("mutation-refinement-proof.yml", "planning"): LIGHT,
    ("access-audit.yml", "audit"): LIGHT,
    ("branch-lanes.yml", "grade"): LIGHT,
    ("hive-worker.yml", "contracts"): LIGHT,
    ("hive-worker.yml", "kind"): STANDARD,
    ("brain-smoke.yml", "smoke"): QUEUED,
    ("codex-cache.yml", "seed"): STANDARD,
    ("coverage-stability.yml", "compare"): BATCH,
    ("equivalence.yml", "ledger-equivalence"): STANDARD,
    ("helm-kind.yml", "kind"): QUEUED,
    ("helm-kind-published.yml", "kind"): STANDARD,
    ("hierarchy-cpu.yml", "grade"): LIGHT,
    ("hierarchy-cpu.yml", "hierarchy"): BATCH,
    ("hierarchy-cpu.yml", "load"): BATCH,
    ("ledger-load.yml", "compare"): QUEUED,
    ("ledger-load.yml", "compare-grade"): LIGHT,
    ("ledger-load.yml", "load"): QUEUED,
    ("ledger-row-memory.yml", "replay"): BATCH,
    ("mutation-preflight.yml", "mutation"): LIGHT,
    ("mutation-preflight.yml", "mutation-shard"): BATCH,
    ("mutation-preflight.yml", "plan"): STANDARD,
    ("mutation-preflight.yml", "refine"): LIGHT,
    ("mutation-replay.yml", "plan"): LIGHT,
    ("mutation-replay.yml", "grade"): BATCH,
    ("mutation-replay.yml", "verify"): LIGHT,
    ("mutation-preflight.yml", "stats"): BATCH,
    ("mutation-preflight.yml", "touched-tests"): STANDARD,
    ("proofs.yml", "operations-cpu"): BATCH,
    ("proofs.yml", "operations-receipts"): BATCH,
    ("proofs.yml", "tests"): STANDARD,
    ("semgrep.yml", "charts"): LIGHT,
    ("semgrep.yml", "feature-tests"): LIGHT,
    ("semgrep.yml", "scan"): LIGHT,
    ("startup-stages.yml", "measure"): STANDARD,
    ("swarm-image.yml", "publish"): STANDARD,
    ("swarm-node-base.yml", "copy"): STANDARD,
    ("swarm-node-image.yml", "image"): STANDARD,
    ("swarm-node-image.yml", "hive"): STANDARD,
    ("swarm-node-smoke.yml", "worker-health"): QUEUED,
    ("swarm-node-smoke.yml", "worker-image"): QUEUED,
    ("swarm-node-smoke.yml", "worker-image-compare"): LIGHT,
    ("swarm-smoke.yml", "compose-hive"): QUEUED,
    ("swarm-smoke.yml", "hive-join"): QUEUED,
    ("swarm-smoke.yml", "hive-launch"): QUEUED,
    ("swarm-smoke.yml", "smoke"): QUEUED,
    ("swarm-smoke.yml", "terminal-clients"): QUEUED,
    ("test-leaks.yml", "followup"): LIGHT,
    ("test-leaks.yml", "one-process"): BATCH,
    ("test.yml", "admission"): LIGHT,
    ("test.yml", "coverage-baseline"): LIGHT,
    ("test.yml", "coverage-ratchet"): LIGHT,
    ("test.yml", "flake-budget"): LIGHT,
    ("test.yml", "gate-required"): LIGHT,
    ("test.yml", "kind-due"): QUEUED,
    ("test.yml", "lint"): QUEUED,
    ("test.yml", "mutation"): BATCH,
    ("test.yml", "mutation-plan"): LIGHT,
    ("test.yml", "mutation-stats"): BATCH,
    ("test.yml", "mutation-stats-seed"): LIGHT,
    ("test.yml", "queue-fail-fast"): LIGHT,
    ("test.yml", "refresh-durations"): QUEUED,
    ("count-record.yml" if (WORKFLOWS / "count-record.yml").exists() else "test.yml", "count-record"): QUEUED,
    ("test.yml", "reuse"): LIGHT,
    ("test.yml", "select"): LIGHT,
    ("test.yml", "shard-check"): LIGHT,
    ("test.yml", "sonar"): QUEUED,
    ("test.yml", "split"): QUEUED,
    ("test.yml", "stage-budget"): LIGHT,
    ("test.yml", "test-count"): QUEUED,
    ("test.yml", "test-summary"): LIGHT,
    ("test.yml", "unit"): QUEUED,
    ("test.yml", "unit-lost"): LIGHT,
    ("test.yml", "unit-rerun"): QUEUED,
    ("test.yml", "unit-selected"): STANDARD,
    ("writer-lock.yml", "inputs"): LIGHT,
    ("writer-lock.yml", "scenario"): QUEUED,
    ("worker-resources.yml", "measure"): BATCH,
}
if (WORKFLOWS / "actions-storage.yml").exists():
    MOVED[("actions-storage.yml", "prune")] = LIGHT
if (WORKFLOWS / "ci-minutes.yml").exists():
    MOVED[("ci-minutes.yml", "measure")] = LIGHT

# These exchange a GitHub OIDC token, which the Anton runners have timed out fetching.
OIDC = {("pages.yml", "deploy"), ("publish-pypi.yml", "publish")}

# The runner trust loader contract fixes this job's runner.
PROTECTED = {("runner-trust.yml", "actors")}

# Runner trust never admits a pull_request_target run to a private runner.
TARGET = {("gate-protected.yml", "grade")}

# The runner class table holds the release path and the access read back on hosted runners.
CLASSED = {
    ("mutation-deadline-proof.yml", "receipts"),
    ("ci-templates.yml", "actionlint"),
    ("pages.yml", "build"),
    ("promote.yml", "promote"),
    ("publish-pypi.yml", "verify"),
    ("release.yml", "release"),
    ("github-access-audit.yml", "read-back"),
    ("queue-retry.yml", "retry"),
    ("lost-runner.yml", "watch"),
}

HOSTED = OIDC | PROTECTED | TARGET | CLASSED
CONSOLIDATED = {
    ("test.yml", "durations"),
    ("swarm-node-smoke.yml", "worker-health"),
    ("swarm-smoke.yml", "compose-hive"),
    ("swarm-smoke.yml", "hive-join"),
    ("swarm-smoke.yml", "hive-launch"),
    ("swarm-smoke.yml", "terminal-clients"),
}


def workflows():
    return {path.name: yaml.safe_load(path.read_text()) for path in sorted(WORKFLOWS.glob("*.yml"))}


def runner_jobs():
    return {
        (name, job): spec
        for name, workflow in workflows().items()
        for job, spec in workflow["jobs"].items()
        if "uses" not in spec
    }


class Null:
    def __getattr__(self, _):
        return self

    def __eq__(self, other):
        return other is None or isinstance(other, Null)

    def __hash__(self):
        return 0


class Context:
    def __init__(self, values):
        self.values = values

    def __getattr__(self, name):
        value = self.values.get(name, Null())
        return Context(value) if isinstance(value, dict) else value


def resolve(runs_on, **github):
    body = re.fullmatch(r"\$\{\{(.*)\}\}", runs_on, re.S)
    if body is None:
        return runs_on
    expression = body.group(1).replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    names = {
        "github": Context(github),
        "fromJSON": json.loads,
        "contains": lambda items, item: item in items,
        "startsWith": lambda text, prefix: str(text).lower().startswith(prefix.lower()),
    }
    return eval(expression, {"__builtins__": {}}, names)


def event(name, actor="nestorcolt", triggering=None, head="the-cloud-clockwork/agentihooks", ref="refs/heads/topic"):
    github = {
        "event_name": name,
        "actor": actor,
        "triggering_actor": triggering or actor,
        "repository": "the-cloud-clockwork/agentihooks",
        "ref": ref,
    }
    if name.startswith("pull_request"):
        github["event"] = {"pull_request": {"head": {"repo": {"full_name": head}}}}
    return github


TRUSTED_EVENTS = [
    event("push"),
    event("pull_request"),
    event("workflow_dispatch"),
    event("schedule"),
    event("pull_request", actor="agenticore-app[bot]"),
    event("merge_group", actor="github-merge-queue[bot]"),
    event("push", actor="github-merge-queue[bot]", ref="refs/heads/dev"),
]

UNTRUSTED_EVENTS = [
    event("pull_request", actor="stranger"),
    event("pull_request", head="stranger/agentihooks"),
    event("pull_request_target"),
    event("pull_request_target", actor="stranger"),
    event("pull_request_target", triggering="stranger"),
    event("pull_request_target", head="stranger/agentihooks"),
    event("pull_request_target", head=None),
    event("push", actor="github-merge-queue[bot]"),
    event("push", actor="github-merge-queue[bot]", triggering="nestorcolt"),
    event("workflow_dispatch", actor="github-merge-queue[bot]", triggering="nestorcolt", ref="refs/heads/dev"),
    event("workflow_dispatch", actor="nestorcolt", triggering="stranger"),
]


@pytest.mark.parametrize(("workflow", "job"), sorted(MOVED))
def test_each_job_runs_on_its_isolated_anton_pool_only_for_trusted_actors(workflow, job):
    if (workflow, job) in CONSOLIDATED and job not in workflows()[workflow]["jobs"]:
        pytest.skip("consolidated into its workflow's remaining smoke job")
    assert workflows()[workflow]["jobs"][job]["runs-on"] == pool(MOVED[(workflow, job)])


def test_test_count_timeout_holds_three_times_its_slowest_recent_run():
    slowest_seconds = 311  # Tests run 38364025608: the 3.12 leg hit the old five minute timeout
    assert workflows()["test.yml"]["jobs"]["test-count"]["timeout-minutes"] * 60 >= 3 * slowest_seconds


def test_no_job_resolves_a_hosted_runner_for_a_trusted_actor():
    hosted = sorted(
        f"{name}:{job} on {github['event_name']} by {github['actor']}"
        for (name, job), spec in runner_jobs().items()
        if (name, job) not in HOSTED
        for github in TRUSTED_EVENTS
        if resolve(str(spec.get("runs-on", "")), **github) not in {LIGHT, STANDARD, BATCH, QUEUE}
    )
    assert hosted == []


@pytest.mark.parametrize("triggering", ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"])
@pytest.mark.parametrize("name", ["push", "merge_group"])
def test_trusted_reruns_of_queue_runs_keep_the_classified_anton_pool(name, triggering):
    github = event(name, actor="github-merge-queue[bot]", triggering=triggering, ref="refs/heads/dev")
    hosted = sorted(
        f"{workflow}:{job}"
        for (workflow, job), spec in runner_jobs().items()
        if (workflow, job) not in HOSTED
        if resolve(str(spec.get("runs-on", "")), **github) != resolve(pool(MOVED[(workflow, job)]), **github)
    )
    assert hosted == []


@pytest.mark.parametrize("name", ["push", "merge_group"])
def test_untrusted_reruns_of_queue_runs_stay_hosted(name):
    github = event(name, actor="github-merge-queue[bot]", triggering="stranger", ref="refs/heads/dev")
    assert all(resolve(str(spec["runs-on"]), **github) == "ubuntu-latest" for spec in runner_jobs().values())


def test_untrusted_and_fork_runs_fall_back_to_hosted_runners():
    leaked = sorted(
        f"{name}:{job} on {github['event_name']} by {github['actor']}"
        for (name, job), spec in runner_jobs().items()
        for github in UNTRUSTED_EVENTS
        if resolve(str(spec.get("runs-on", "")), **github) != "ubuntu-latest"
    )
    assert leaked == []


def test_every_hosted_job_stays_on_a_hosted_runner():
    jobs = runner_jobs()
    for name, job in OIDC:
        permissions = {**workflows()[name].get("permissions", {}), **jobs[(name, job)].get("permissions", {})}
        assert permissions.get("id-token") == "write", f"{name}:{job}"
        assert jobs[(name, job)]["runs-on"] == "ubuntu-latest", f"{name}:{job}"
    for name, job in PROTECTED | TARGET | CLASSED:
        assert jobs[(name, job)]["runs-on"] == "ubuntu-latest", f"{name}:{job}"
    assert set(jobs) | CONSOLIDATED == set(MOVED) | HOSTED | CONSOLIDATED


def test_jobs_with_service_or_job_containers_run_on_the_docker_pool():
    for (name, job), spec in runner_jobs().items():
        if "services" in spec or "container" in spec:
            assert MOVED.get((name, job)) in {STANDARD, QUEUED}, f"{name}:{job}"


def test_the_expression_reader_tells_trusted_from_untrusted_runs():
    assert resolve(pool(LIGHT), **event("pull_request")) == LIGHT
    assert resolve(pool(LIGHT), **event("pull_request", head="stranger/agentihooks")) == "ubuntu-latest"
    assert resolve(pool(LIGHT), **event("pull_request_target")) == "ubuntu-latest"
    assert resolve("ubuntu-latest", **event("push")) == "ubuntu-latest"
    assert resolve(pool(QUEUED), **event("merge_group", actor="github-merge-queue[bot]")) == QUEUE
    assert resolve(pool(QUEUED), **event("pull_request")) == STANDARD
    assert resolve(pool(QUEUED), **event("pull_request_target")) == "ubuntu-latest"


def test_every_standard_job_the_tests_run_calls_sends_merge_group_runs_to_the_queue_pool():
    called = {spec["uses"].rsplit("/", 1)[1] for spec in workflows()["test.yml"]["jobs"].values() if "uses" in spec}
    jobs = runner_jobs()
    standard = {key for key, spec in jobs.items() if key[0] in called and "'anton-oss'" in spec["runs-on"]}
    assert standard
    assert {key: MOVED[key] for key in standard} == dict.fromkeys(standard, QUEUED)
    merge_group = event("merge_group", actor="github-merge-queue[bot]")
    for key in standard:
        runs_on = jobs[key]["runs-on"]
        assert resolve(runs_on, **merge_group) == QUEUE, key
        assert resolve(runs_on, **event("pull_request")) == STANDARD, key
        assert resolve(runs_on, **event("push", actor="github-merge-queue[bot]", ref="refs/heads/dev")) == STANDARD, key
