from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import static_exclusions
from scripts.ci_lab.units import Priority

pytestmark = [pytest.mark.unit, pytest.mark.feature("lab-static-execution-exclusions")]

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


def test_the_policy_preserves_each_declared_reason() -> None:
    assert static_exclusions.read(POLICY) == {NETWORK: REASON}


@pytest.mark.parametrize(
    "raw",
    [
        "[",
        "[]",
        json.dumps({NETWORK: []}),
        json.dumps({NETWORK: {}}),
        json.dumps({NETWORK: {"reason": ""}}),
        json.dumps({NETWORK: {"reason": 7}}),
        json.dumps({"": {"reason": REASON}}),
    ],
)
def test_invalid_exclusion_records_are_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        static_exclusions.read(raw)


@pytest.mark.parametrize("tier", [Priority.HOTFIX, Priority.PULL_REQUEST])
def test_only_the_protected_catalog_can_exclude_units(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]], tier: Priority
) -> None:
    plan, base, _ = expand(POLICY, "not json", tier, ())
    assert members(plan) == {OFFLINE, TYPES}
    assert plan.grader == base


def test_a_missing_protected_catalog_preserves_units_and_ignores_the_head_catalog(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    plan, _, _ = expand(None, POLICY, Priority.PULL_REQUEST, ())
    assert members(plan) == {NETWORK, OFFLINE, TYPES}


def test_invalid_protected_policy_refuses_expansion(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    with pytest.raises(ValueError):
        expand(json.dumps({NETWORK: {}}), None, Priority.PULL_REQUEST, ())


def test_a_partially_graded_job_keeps_its_actions_authority(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]],
) -> None:
    plan, _, _ = expand(POLICY, None, Priority.PULL_REQUEST, ("lint", "type-gate"))
    assert members(plan) == {OFFLINE, TYPES}
    assert plan.authoritative == frozenset({"type-gate"})


@pytest.mark.parametrize("tier", [Priority.PROMOTION, Priority.BACKFILL])
def test_integrated_grades_use_the_integrated_head_policy(
    expand: Callable[[str | None, str | None, Priority, tuple[str, ...]], tuple[Any, str, str]], tier: Priority
) -> None:
    policy = json.dumps({OFFLINE: {"needs": ["network"], "reason": REASON}})
    plan, _, head = expand(POLICY, policy, tier, ())
    assert members(plan) == {NETWORK, TYPES}
    assert plan.grader == head
