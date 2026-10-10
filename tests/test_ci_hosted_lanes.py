from itertools import product
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github/workflows"

LANE = (
    "format('hosted-lane-{0}', "
    "((endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || endsWith(github.run_id, '2')) && 'a') || "
    "((endsWith(github.run_id, '3') || endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b') || 'c')"
)

DIRECT = {
    "helm-kind.yml": ("Helm chart in kind", "helm-kind"),
    "ledger-load.yml": ("Ledger server load", "ledger-load"),
    "swarm-smoke.yml": ("Swarm container smoke", "swarm-smoke"),
    "swarm-node-smoke.yml": ("Swarm worker image smoke", "swarm-node-smoke"),
    "brain-smoke.yml": ("brain-smoke", "brain-smoke"),
}
LANE_USERS = {
    "test.yml",
    "proofs.yml",
    "test-leaks.yml",
    "swarm-node-image.yml",
    "mutation-preflight.yml:mutation",
    *DIRECT,
}
EXCLUDED = {
    "coverage-stability.yml": "one short job that reads dev run data",
    "pages.yml": "publishes docs from main",
    "publish-pypi.yml": "release path",
    "release.yml": "release path",
    "swarm-image.yml": "its jobs run only on a dev push",
}
EVENTS = ("pull_request", "merge_group", "push", "workflow_dispatch", "schedule")
REFS = ("refs/heads/dev", "refs/heads/diffcheck/plant", "refs/heads/task-branch")
CALLERS = ("Tests", "Proofs")


class _Context(dict):
    def __getattr__(self, key):
        value = self.get(key, _Context())
        return _Context(value) if isinstance(value, dict) else value


def _evaluate(value, **github):
    if not isinstance(value, str) or not value.startswith("${{"):
        return value
    body = value.removeprefix("${{").removesuffix("}}").replace("&&", " and ").replace("||", " or ")
    names = {
        "github": _Context(github),
        "endsWith": lambda text, end: str(text).endswith(end),
        "format": lambda template, *args: template.format(*args),
    }
    return eval(body, {"__builtins__": {}}, names)


def _workflow(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _blocks():
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        if "concurrency" in workflow:
            yield path.name, workflow["name"], workflow["concurrency"]
        for key, job in workflow.get("jobs", {}).items():
            if isinstance(job, dict) and "concurrency" in job:
                yield f"{path.name}:{key}", workflow["name"], job["concurrency"]


def _block(where):
    return next(block for name, _, block in _blocks() if name == where)


def _resolve(block, **github):
    return {key: _evaluate(block.get(key), **github) for key in ("group", "queue", "cancel-in-progress")}


def _contexts(title):
    for event, ref, workflow, run_id in product(EVENTS, REFS, (title, *CALLERS), range(1000, 1010)):
        yield {"event_name": event, "ref": ref, "workflow": workflow, "run_id": run_id}


def _is_lane(group):
    return str(group).startswith("hosted-lane-")


def test_every_non_required_trigger_is_on_a_lane_or_excluded_with_a_reason():
    open_triggers = set()
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        events = yaml.safe_load(path.read_text())[True]
        push = events.get("push") or {}
        if "workflow_dispatch" in events or ("push" in events and push.get("branches") != ["dev"]):
            open_triggers.add(path.name)
    users = {where.split(":")[0] for where in LANE_USERS}
    assert not users & set(EXCLUDED)
    assert open_triggers <= users | set(EXCLUDED)
    assert {where for where, _, block in _blocks() if "hosted-lane" in str(block["group"])} == LANE_USERS


def test_every_lane_user_names_the_same_three_lanes():
    for where, _, block in _blocks():
        if "hosted-lane" in str(block["group"]):
            assert str(block["group"]).count("hosted-lane") == 1, where
            assert LANE in block["group"], where


def test_a_run_on_a_lane_queues_and_is_never_cancelled():
    seen = set()
    for where, title, block in _blocks():
        for github in _contexts(title):
            resolved = _resolve(block, **github)
            if _is_lane(resolved["group"]):
                seen.add(where)
                assert resolved["queue"] == "max", (where, github)
                assert not resolved["cancel-in-progress"], (where, github)
            else:
                assert resolved["queue"] in (None, "single"), (where, github)
    assert seen == LANE_USERS


def test_the_run_id_last_digit_spreads_runs_over_three_lanes():
    block = _block("proofs.yml")
    lanes = [_resolve(block, run_id=run_id)["group"] for run_id in range(1000, 1010)]
    assert lanes == ["hosted-lane-a"] * 3 + ["hosted-lane-b"] * 3 + ["hosted-lane-c"] * 4


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push"])
def test_required_runs_and_their_callees_never_reach_a_lane(event):
    github = {"event_name": event, "ref": "refs/heads/dev", "workflow": "Tests", "run_id": 1000}
    assert not _is_lane(_resolve(_block("test.yml"), **github)["group"])
    for name, (_, own) in DIRECT.items():
        assert _resolve(_block(name), **github)["group"] == f"{own}-1000"
    assert not _is_lane(_resolve(_block("swarm-node-image.yml"), **github)["group"])


@pytest.mark.parametrize("caller", CALLERS)
def test_a_called_workflow_never_waits_on_its_callers_lane(caller):
    github = {
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/diffcheck/plant",
        "workflow": caller,
        "run_id": 1000,
    }
    assert _is_lane(_resolve(_block("test.yml" if caller == "Tests" else "proofs.yml"), **github)["group"])
    for name, (_, own) in DIRECT.items():
        assert _resolve(_block(name), **github) == {
            "group": f"{own}-1000",
            "queue": "single",
            "cancel-in-progress": None,
        }
    mutation = _resolve(_block("mutation-preflight.yml:mutation"), **github)
    assert mutation["group"] == "mutation-preflight-job-1000"
    assert mutation["group"] != _resolve(_block("mutation-preflight.yml"), **github)["group"]


def test_direct_dispatches_and_branch_pushes_queue_on_a_lane():
    dispatch = {"event_name": "workflow_dispatch", "ref": "refs/heads/diffcheck/plant", "run_id": 1000}
    for name, (title, _) in DIRECT.items():
        assert _is_lane(_resolve(_block(name), workflow=title, **dispatch)["group"]), name
    assert _is_lane(_resolve(_block("test-leaks.yml"), workflow="Test leaks", **dispatch)["group"])
    assert _is_lane(_resolve(_block("swarm-node-image.yml"), workflow="Swarm worker image", **dispatch)["group"])
    push = {"event_name": "push", "ref": "refs/heads/task-branch", "workflow": "Mutation preflight", "run_id": 1000}
    assert _is_lane(_resolve(_block("mutation-preflight.yml:mutation"), **push)["group"])
    schedule = {"event_name": "schedule", "ref": "refs/heads/dev", "workflow": "Test leaks", "run_id": 1000}
    assert _resolve(_block("test-leaks.yml"), **schedule) == {
        "group": "test-leaks-refs/heads/dev",
        "queue": "single",
        "cancel-in-progress": True,
    }


def test_lane_titles_match_the_workflow_names():
    for name, (title, _) in DIRECT.items():
        assert _workflow(name)["name"] == title, name
