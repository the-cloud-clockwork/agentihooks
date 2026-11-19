import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts.ci_lab import checks, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.units import Priority
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    CHECK_RUNS,
    DELIVERY,
    HEAD_A,
    SLUG,
    FixtureToken,
    Run,
    _commit,
    _no_tests,
    _unknown,
    github,
    plan,
    pull_plan,
    restarted,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin

LISTED = frozenset({"lint"})


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_merge_group_grade_takes_the_queue_list_and_a_pull_request_or_hotfix_grade_the_lab_list() -> None:
    block = {"authoritative": ["lint"], "queue": {"full_suite_entries": 5, "authoritative": ["unit"]}}
    for tier in (Priority.HOTFIX, Priority.PULL_REQUEST):
        assert scheduler.authoritative_gates(block, tier) == LISTED
    assert scheduler.authoritative_gates(block, Priority.QUEUE) == frozenset({"unit"})
    unlisted = {"authoritative": ["lint"], "queue": {"full_suite_entries": 5}}
    assert scheduler.authoritative_gates(unlisted, Priority.QUEUE) == frozenset()
    assert scheduler.authoritative_gates({"authoritative": ["lint"]}, Priority.QUEUE) == frozenset()


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_gate_the_grade_lists_as_authoritative_posts_as_lab_and_every_other_check_as_lab_shadow(
    lab: Callable[..., Run],
) -> None:
    lint, docs = unit(1), unit(2)
    run = lab({HEAD_A: replace(pull_plan({"lint": (lint,), "docs": (docs,)}), authoritative=LISTED)})
    run.cache(lint)
    run.cache(docs)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    names = [(gate, checks.name(gate, shadow)) for _, gate, _, _, _, shadow in run.lab.poster.posts]
    assert names == [("lint", "lab/lint"), ("docs", "lab-shadow/docs"), (scheduler.REQUIRED, "lab-shadow/required")]
    assert run.grade(HEAD_A).authoritative == ["lint"]


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_restarted_scheduler_seeds_each_check_run_under_the_name_the_grade_opened_it_with(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    seeds: list[tuple[Any, ...]] = []
    seed = checks.Checks.seed

    def spy(self: checks.Checks, *args: Any) -> None:
        seeds.append(args)
        seed(self, *args)

    monkeypatch.setattr(requests, "request", github(calls))
    monkeypatch.setattr(checks.Checks, "seed", spy)
    listed = replace(plan({"lint": (unit(1),)}), authoritative=LISTED)
    first = restarted(lab({HEAD_A: listed}), checks.Checks(SLUG, FixtureToken()))
    first.push(HEAD_A, event="pull_request", action="synchronize")
    first.tick(1000)
    grade = first.grade(HEAD_A)
    again = restarted(first, checks.Checks(SLUG, FixtureToken()))
    again.lease()
    again.tick(1100)
    assert calls == [
        ("POST", CHECK_RUNS),
        ("POST", CHECK_RUNS),
        ("PATCH", f"{CHECK_RUNS}/1"),
        ("PATCH", f"{CHECK_RUNS}/2"),
    ]
    assert seeds == [
        (HEAD_A, "lint", grade.run, 1, "queued", False),
        (HEAD_A, scheduler.REQUIRED, grade.run, 2, "queued", True),
    ]


@pytest.mark.feature("lab-authoritative-check-names")
def test_the_mirror_expander_takes_the_authoritative_gates_of_the_grader_delivery_for_the_grade_class(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, base, _ = origin
    listing = _commit(repo, {"delivery.yaml": json.dumps({"lab": {**DELIVERY["lab"], "authoritative": ["lint"]}})})
    head = _commit(repo, {"src/a.py": "a = 3\n"})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)

    def gates(head: str, base: str, tier: Priority) -> frozenset[str]:
        return expander.expand(GradeRequest(f"file://{repo}", head, base, tier, ACTOR, False)).authoritative

    assert gates(head, listing, Priority.PULL_REQUEST) == gates(head, listing, Priority.HOTFIX) == LISTED
    assert gates(listing, base, Priority.PULL_REQUEST) == frozenset()
    assert gates(listing, base, Priority.PROMOTION) == frozenset()
