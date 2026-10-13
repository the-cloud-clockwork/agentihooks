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
    " && github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]')"
)


def pool(label):
    return "${{ " + TRUSTED + " && '" + label + "' || 'ubuntu-latest' }}"


LIGHT, STANDARD, BATCH = "anton-oss-light", "anton-oss", "anton-oss-batch"

MOVED = {
    ("access-audit.yml", "audit"): LIGHT,
    ("hive-worker.yml", "contracts"): LIGHT,
    ("hive-worker.yml", "kind"): STANDARD,
    ("hive-worker.yml", "publish"): STANDARD,
    ("brain-smoke.yml", "smoke"): STANDARD,
    ("coverage-stability.yml", "compare"): BATCH,
    ("equivalence.yml", "ledger-equivalence"): STANDARD,
    ("helm-kind.yml", "kind"): STANDARD,
    ("ledger-load.yml", "load"): STANDARD,
    ("ledger-row-memory.yml", "replay"): BATCH,
    ("mutation-preflight.yml", "mutation"): LIGHT,
    ("mutation-preflight.yml", "mutation-shard"): BATCH,
    ("mutation-preflight.yml", "plan"): LIGHT,
    ("mutation-preflight.yml", "pull-request"): LIGHT,
    ("mutation-preflight.yml", "stats"): BATCH,
    ("proofs.yml", "tests"): STANDARD,
    ("semgrep.yml", "scan"): LIGHT,
    ("startup-stages.yml", "measure"): STANDARD,
    ("swarm-image.yml", "publish"): STANDARD,
    ("swarm-node-base.yml", "copy"): STANDARD,
    ("swarm-node-image.yml", "image"): STANDARD,
    ("swarm-node-smoke.yml", "worker-health"): STANDARD,
    ("swarm-node-smoke.yml", "worker-image"): STANDARD,
    ("swarm-smoke.yml", "compose-hive"): STANDARD,
    ("swarm-smoke.yml", "hive-join"): STANDARD,
    ("swarm-smoke.yml", "hive-launch"): STANDARD,
    ("swarm-smoke.yml", "smoke"): STANDARD,
    ("swarm-smoke.yml", "terminal-clients"): STANDARD,
    ("test-leaks.yml", "followup"): LIGHT,
    ("test-leaks.yml", "one-process"): BATCH,
    ("test.yml", "admission"): LIGHT,
    ("test.yml", "coverage-baseline"): LIGHT,
    ("test.yml", "coverage-ratchet"): STANDARD,
    ("test.yml", "durations"): LIGHT,
    ("test.yml", "flake-budget"): LIGHT,
    ("test.yml", "kind-due"): LIGHT,
    ("test.yml", "lint"): STANDARD,
    ("test.yml", "mutation"): BATCH,
    ("test.yml", "mutation-plan"): LIGHT,
    ("test.yml", "mutation-stats"): BATCH,
    ("test.yml", "queue-baseline"): STANDARD,
    ("test.yml", "refresh-durations"): STANDARD,
    ("test.yml", "shard-check"): LIGHT,
    ("test.yml", "sonar"): STANDARD,
    ("test.yml", "split"): STANDARD,
    ("test.yml", "stage-budget"): LIGHT,
    ("test.yml", "test-count"): LIGHT,
    ("test.yml", "unit"): STANDARD,
    ("worker-resources.yml", "measure"): BATCH,
}

# These exchange a GitHub OIDC token, which the Anton runners have timed out fetching.
OIDC = {("pages.yml", "deploy"), ("publish-pypi.yml", "publish")}

# The protected grader refuses a head that changes these, so they move only through the gate change protocol.
PROTECTED = {("test.yml", "gate-required"), ("test.yml", "reuse"), ("runner-trust.yml", "actors")}

# Runner trust never admits a pull_request_target run to a private runner.
TARGET = {("gate-protected.yml", "grade")}

# The runner class table holds the release path and the access read back on hosted runners.
CLASSED = {
    ("pages.yml", "build"),
    ("publish-pypi.yml", "verify"),
    ("release.yml", "release"),
    ("github-access-audit.yml", "read-back"),
}

HOSTED = OIDC | PROTECTED | TARGET | CLASSED


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
    event("workflow_dispatch", actor="nestorcolt", triggering="stranger"),
]


@pytest.mark.parametrize(("workflow", "job"), sorted(MOVED))
def test_each_job_runs_on_its_isolated_anton_pool_only_for_trusted_actors(workflow, job):
    assert workflows()[workflow]["jobs"][job]["runs-on"] == pool(MOVED[(workflow, job)])


def test_no_job_resolves_a_hosted_runner_for_a_trusted_actor():
    hosted = sorted(
        f"{name}:{job} on {github['event_name']} by {github['actor']}"
        for (name, job), spec in runner_jobs().items()
        if (name, job) not in HOSTED
        for github in TRUSTED_EVENTS
        if resolve(str(spec.get("runs-on", "")), **github) not in {LIGHT, STANDARD, BATCH}
    )
    assert hosted == []


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
    assert set(jobs) == set(MOVED) | HOSTED


def test_jobs_with_service_or_job_containers_run_on_the_docker_pool():
    for (name, job), spec in runner_jobs().items():
        if "services" in spec or "container" in spec:
            assert MOVED.get((name, job)) == STANDARD, f"{name}:{job}"


def test_the_expression_reader_tells_trusted_from_untrusted_runs():
    assert resolve(pool(LIGHT), **event("pull_request")) == LIGHT
    assert resolve(pool(LIGHT), **event("pull_request", head="stranger/agentihooks")) == "ubuntu-latest"
    assert resolve(pool(LIGHT), **event("pull_request_target")) == "ubuntu-latest"
    assert resolve("ubuntu-latest", **event("push")) == "ubuntu-latest"
