from functools import cache
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
    "coverage-stability.yml": "a manual dispatch runs one short job that only reads dev run data",
    "pages.yml": "a rare manual docs build and deploy; the github-pages environment decides which branches deploy",
    "publish-pypi.yml": "operator release path from main",
    "release.yml": "operator release path; a dry run tags and pushes nothing",
    "swarm-image.yml": "a manual dispatch runs no job; its jobs are gated to a dev push",
}
CALLERS = ("test.yml", "proofs.yml")
REQUIRED = ("pull_request", "merge_group", "pull_request_target")
REFS = ("refs/heads/dev", "refs/heads/diffcheck/plant", "refs/heads/task-branch")


class _Context(dict):
    def __getattr__(self, key):
        value = self.get(key, _Context())
        return _Context(value) if isinstance(value, dict) else value


def _evaluate(value, **github):
    if not isinstance(value, str) or not value.startswith("${{"):
        return value
    names = {
        "github": _Context(github),
        "endsWith": lambda text, end: str(text).endswith(end),
        "format": lambda template, *args: template.format(*args),
    }
    return eval(_compiled(value), {"__builtins__": {}}, names)


@cache
def _compiled(value):
    body = value.removeprefix("${{").removesuffix("}}").replace("&&", " and ").replace("||", " or ")
    return compile(body.strip(), "<expression>", "eval")


@cache
def _workflow(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


@cache
def _all_blocks():
    found = []
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        workflow = _workflow(path.name)
        if "concurrency" in workflow:
            found.append((path.name, workflow["concurrency"]))
        for key, job in workflow.get("jobs", {}).items():
            if isinstance(job, dict) and "concurrency" in job:
                found.append((f"{path.name}:{key}", job["concurrency"]))
    return tuple(found)


def _blocks():
    return _all_blocks()


def _block(where):
    return next(block for name, block in _blocks() if name == where)


def _resolve(block, **github):
    return {key: _evaluate(block.get(key), **github) for key in ("group", "queue", "cancel-in-progress")}


def _triggered(events):
    for event, spec in events.items():
        if event == "workflow_call":
            continue
        spec = spec if event == "push" and spec else {}
        refs = [f"refs/heads/{b.replace('**', 'plant')}" for b in spec.get("branches", [])] or [
            ref for ref in REFS if ref.removeprefix("refs/heads/") not in spec.get("branches-ignore", [])
        ]
        for ref in refs:
            yield event, ref


def _contexts(where):
    workflow = _workflow(where.split(":")[0])
    runners = [(workflow["name"], workflow[True])]
    if "workflow_call" in workflow[True]:
        runners += [(_workflow(caller)["name"], _workflow(caller)[True]) for caller in CALLERS]
    for title, events in runners:
        for (event, ref), run_id in product(_triggered(events), (1000, 1003, 1006)):
            yield {"event_name": event, "ref": ref, "workflow": title, "run_id": run_id}


def _is_lane(group):
    return str(group).startswith("hosted-lane-")


def _open(events):
    push = events.get("push") or {}
    return "workflow_dispatch" in events or ("push" in events and push.get("branches") != ["dev"])


def test_every_non_required_trigger_is_on_a_lane_or_excluded_with_a_reason():
    open_triggers = {path.name for path in _WORKFLOWS.glob("*.yml") if _open(yaml.safe_load(path.read_text())[True])}
    users = {where.split(":")[0] for where in LANE_USERS}
    assert not users & set(EXCLUDED)
    assert set(EXCLUDED) <= open_triggers
    assert open_triggers <= users | set(EXCLUDED)
    assert {where for where, block in _blocks() if "hosted-lane" in str(block["group"])} == LANE_USERS


def test_every_lane_user_names_the_same_three_lanes():
    for where, block in _blocks():
        if "hosted-lane" in str(block["group"]):
            assert str(block["group"]).count("hosted-lane") == 1, where
            assert LANE in block["group"], where


def test_a_run_on_a_lane_queues_and_is_never_cancelled():
    seen = set()
    for where, block in _blocks():
        for github in _contexts(where):
            resolved = _resolve(block, **github)
            if _is_lane(resolved["group"]):
                seen.add(where)
                assert resolved["queue"] == "max", (where, github)
                assert not resolved["cancel-in-progress"], (where, github)
            else:
                assert resolved["queue"] in (None, "single"), (where, github)
    assert seen == LANE_USERS


def test_no_required_run_or_its_callee_ever_reaches_a_lane():
    checked = 0
    for where, block in _blocks():
        for github in _contexts(where):
            if github["event_name"] in REQUIRED or (github["event_name"], github["ref"]) == ("push", "refs/heads/dev"):
                checked += 1
                assert not _is_lane(_resolve(block, **github)["group"]), (where, github)
    assert checked


def test_the_run_id_last_digit_spreads_runs_over_three_lanes():
    block = _block("proofs.yml")
    lanes = [_resolve(block, run_id=run_id)["group"] for run_id in range(1000, 1010)]
    assert lanes == ["hosted-lane-a"] * 3 + ["hosted-lane-b"] * 3 + ["hosted-lane-c"] * 4


@pytest.mark.parametrize("caller", ["Tests", "Proofs"])
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


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_only_the_dev_push_worker_image_build_stays_uncapped_because_it_publishes_the_deploy_image(event):
    github = {"event_name": event, "ref": "refs/heads/dev", "workflow": "Swarm worker image", "run_id": 1000}
    resolved = _resolve(_block("swarm-node-image.yml"), **github)
    if event == "push":
        assert resolved == {"group": "swarm-node-image-refs/heads/dev", "queue": "single", "cancel-in-progress": False}
    else:
        assert resolved == {"group": "hosted-lane-a", "queue": "max", "cancel-in-progress": False}


def test_lane_titles_match_the_workflow_names():
    for name, (title, _) in DIRECT.items():
        assert _workflow(name)["name"] == title, name
