from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import static_exclusions
from scripts.ci_lab.units import Priority

pytestmark = pytest.mark.unit

NETWORK = "lint/network"
OFFLINE = "lint/offline"
TYPES = "type-gate/offline"
REASON = "the worker has no network"
POLICY = json.dumps({NETWORK: {"needs": ["network"], "reason": REASON}})


@pytest.fixture
def expand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]]:
    from scripts.ci_lab import scheduler
    from scripts.ci_lab.digests import Toolchain
    from scripts.dep_manifest import _git
    from tests.ci_lab.test_scheduler import ACTOR, DELIVERY, LOCK, _commit, _gate, _no_tests, _unknown

    def build(
        policy: str | None,
        head_policy: str | None,
        tier: Priority,
        authoritative: tuple[str, ...],
    ) -> tuple[Any, str, str]:
        repo = tmp_path / "origin"
        _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
        delivery = copy.deepcopy(DELIVERY)
        delivery["lab"]["authoritative"] = list(authoritative)
        files = {
            ".github/workflows/test.yml": "jobs: {}\n",
            ".github/input-keys.json": "{}",
            "delivery.yaml": json.dumps(delivery),
            LOCK: "lock",
            "pyproject.toml": "[project]\n",
            "src/example.py": "value = 1\n",
        }
        if policy is not None:
            files[static_exclusions.PATH] = policy
        base = _commit(repo, files)
        changed = {"src/example.py": "value = 2\n"}
        if head_policy is not None:
            changed[static_exclusions.PATH] = head_policy
        head = _commit(repo, changed)
        gates = [
            _gate(NETWORK, "lint", ("src",), ()),
            _gate(OFFLINE, "lint", ("src",), ()),
            _gate(TYPES, "type-gate", ("src",), ()),
        ]
        monkeypatch.setattr(scheduler, "discover", lambda *_: gates)
        node = tmp_path / "node"
        _git(tmp_path, "init", "-q", "--bare", str(node))
        toolchain = Toolchain("3.12", {}, "", "", "image", {})
        expander = scheduler.MirrorExpander(node, tmp_path / "mirrors", toolchain, _no_tests, _unknown)
        monkeypatch.setattr(expander, "_mutation", lambda *_: {})
        request = scheduler.GradeRequest(f"file://{repo}", head, base, tier, ACTOR, False)
        return expander.expand(request), base, head

    return build


def members(plan: Any) -> set[str]:
    return {name for units in plan.gates.values() for unit in units for name in unit.members}


@pytest.mark.feature("lab-static-execution-exclusions")
def test_the_policy_preserves_each_declared_reason() -> None:
    assert static_exclusions.read(POLICY) == {NETWORK: REASON}
    records = json.loads(POLICY)
    records[OFFLINE] = {"reason": "browser is unavailable"}
    assert static_exclusions.read(json.dumps(records)) == {NETWORK: REASON, OFFLINE: "browser is unavailable"}


@pytest.mark.parametrize(
    "raw,diagnostic",
    [
        ("[", "Expecting value"),
        ("[]", "catalog"),
        (json.dumps({NETWORK: []}), "record"),
        (json.dumps({NETWORK: {}}), "reason"),
        (json.dumps({NETWORK: {"reason": ""}}), "reason"),
        (json.dumps({NETWORK: {"reason": "  "}}), "reason"),
        (json.dumps({NETWORK: {"reason": 7}}), "reason"),
        (json.dumps({"": {"reason": REASON}}), "gate"),
        (json.dumps({"  ": {"reason": REASON}}), "gate"),
    ],
)
@pytest.mark.feature("lab-static-execution-exclusions")
def test_invalid_exclusion_records_are_refused(raw: str, diagnostic: str) -> None:
    with pytest.raises(ValueError, match=diagnostic):
        static_exclusions.read(raw)


@pytest.mark.parametrize("tier", [Priority.HOTFIX, Priority.PULL_REQUEST])
@pytest.mark.feature("lab-static-execution-exclusions")
def test_only_the_protected_catalog_can_exclude_units(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]], tier: Priority
) -> None:
    plan, base, _ = expand(POLICY, "not json", tier, ())
    assert members(plan) == {OFFLINE, TYPES}
    assert plan.grader == base


@pytest.mark.feature("lab-static-execution-exclusions")
def test_a_missing_protected_catalog_preserves_units_and_ignores_the_head_catalog(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    plan, _, _ = expand(None, POLICY, Priority.PULL_REQUEST, ("lint", "type-gate"))
    assert members(plan) == {NETWORK, OFFLINE, TYPES}
    assert plan.authoritative == frozenset({"lint", "type-gate"})


@pytest.mark.feature("lab-static-execution-exclusions")
def test_invalid_protected_policy_refuses_expansion(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    with pytest.raises(ValueError, match="reason"):
        expand(json.dumps({NETWORK: {}}), None, Priority.PULL_REQUEST, ())


@pytest.mark.feature("lab-static-execution-exclusions")
def test_a_partially_graded_job_keeps_its_actions_authority(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    plan, _, _ = expand(POLICY, None, Priority.PULL_REQUEST, ("lint", "type-gate"))
    assert members(plan) == {OFFLINE, TYPES}
    assert plan.authoritative == frozenset({"type-gate"})


@pytest.mark.feature("lab-static-execution-exclusions")
def test_reusable_step_exclusions_remove_the_calling_jobs_authority(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    policy = json.dumps({"semgrep/charts/helm": {"reason": "docker"}})
    plan, _, _ = expand(policy, None, Priority.PULL_REQUEST, ("lint", "semgrep", "type-gate"))
    assert members(plan) == {NETWORK, OFFLINE, TYPES}
    assert plan.authoritative == frozenset({"lint", "type-gate"})


@pytest.mark.parametrize("tier", [Priority.PROMOTION, Priority.BACKFILL])
@pytest.mark.feature("lab-static-execution-exclusions")
def test_integrated_grades_use_the_integrated_head_policy(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]], tier: Priority
) -> None:
    policy = json.dumps({OFFLINE: {"needs": ["network"], "reason": REASON}})
    plan, _, head = expand(POLICY, policy, tier, ())
    assert members(plan) == {NETWORK, TYPES}
    assert plan.grader == head


@pytest.fixture
def authority_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], Path]:
    from tests.ci_lab.test_delivery import block, tree

    def build(policy: str | None) -> Path:
        configured = ["lint", "semgrep", "unit"]
        grader = tree(
            tmp_path / "grader",
            block(authoritative=configured, queue={"authoritative": configured}),
        )
        (grader / ".github/workflows/test.yml").write_text(json.dumps({"jobs": {job: {} for job in configured}}))
        catalog = grader / static_exclusions.PATH
        catalog.parent.mkdir(parents=True)
        if policy is not None:
            catalog.write_text(policy)
        head = tmp_path / "head"
        (head / static_exclusions.PATH).parent.mkdir(parents=True)
        (head / static_exclusions.PATH).write_text(json.dumps({"unit/step": {"reason": "head only"}}))
        monkeypatch.chdir(head)
        return grader

    return build


@pytest.mark.feature("lab-static-execution-exclusions")
@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (None, ["lint", "semgrep", "unit"]),
        ("{}", ["lint", "semgrep", "unit"]),
        (json.dumps({"linting/step": {"reason": "different job"}}), ["lint", "semgrep", "unit"]),
        (
            json.dumps(
                {
                    "lint/network": {"reason": "network"},
                    "semgrep/charts/helm": {"reason": "docker"},
                }
            ),
            ["unit"],
        ),
    ],
)
def test_actions_and_scheduler_share_the_protected_authority_policy(
    authority_tree: Callable[[str | None], Path], policy: str | None, expected: list[str]
) -> None:
    from scripts import ci_lab_verdict
    from scripts.ci_lab import delivery

    grader = authority_tree(policy)
    assert delivery.authoritative(grader) == tuple(expected)
    assert delivery.cutover(grader, {}) == expected
    assert delivery.queue_cutover(grader, {}) == expected
    for event in ("pull_request", "merge_group"):
        output = ci_lab_verdict.authority(grader, event, {})
        assert json.loads(output["authoritative"]) == {job: True for job in expected}


@pytest.mark.feature("lab-static-execution-exclusions")
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_actions_refuses_malformed_protected_exclusions(
    authority_tree: Callable[[str | None], Path], event: str
) -> None:
    from scripts import ci_lab_verdict

    grader = authority_tree('{"lint/network": {}}')
    with pytest.raises(ValueError, match="reason"):
        ci_lab_verdict.authority(grader, event, {})


@pytest.mark.feature("lab-static-execution-exclusions")
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_lab_off_keeps_actions_ownership_even_with_invalid_policy(
    authority_tree: Callable[[str | None], Path], event: str
) -> None:
    from scripts import ci_lab_verdict
    from scripts.ci_lab import delivery

    grader = authority_tree("invalid")
    output = ci_lab_verdict.authority(grader, event, {delivery.LAB_OFF: "true"})
    assert json.loads(output["authoritative"]) == {}
