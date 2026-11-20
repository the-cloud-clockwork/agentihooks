import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts.ci_lab import checks, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    CHECK_RUNS,
    DELIVERY,
    HEAD_A,
    SLUG,
    SUITE_MANIFEST,
    TREE,
    FakeKube,
    FixtureToken,
    Inbox,
    Port,
    Poster,
    Run,
    _commit,
    _gate,
    _no_tests,
    _suite,
    _unknown,
    fake_redis,
    github,
    plan,
    restarted,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin
from tests.ci_lab.test_scheduler import staticless as staticless

LISTING = json.dumps({"lab": {**DELIVERY["lab"], "authoritative": ["lint"]}})


def graded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path, heads: list[tuple[str, str, str]]
) -> dict[str, list[str]]:
    gates = [_gate("size", "lint", ("src",), ()), _gate("pages", "docs", ("docs",), ())]
    monkeypatch.setattr(scheduler, "discover", lambda workflow, registry, reusable: gates)
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    inbox, poster = Inbox(), Poster()
    ports = scheduler.Ports(inbox, expander, lambda _repo: poster, frozenset[str], FakeKube())
    settings = scheduler.Settings(frozenset({ACTOR}), 0)
    engine = scheduler.Scheduler(fake_redis(), LocalObjects(tmp_path / "objects"), ports, settings)
    for lane, (event, head, base) in enumerate(heads):
        pushed = HeadEvent(event, f"file://{repo}", head, base, ACTOR, "synchronize")
        inbox.pending.append(scheduler.Head(pushed, str(lane), ""))
    engine.tick(1000)
    names: dict[str, list[str]] = {}
    for head, gate, _, _, _, shadow in poster.posts:
        names.setdefault(head, []).append(checks.name(gate, shadow))
    return names


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_pull_request_grade_posts_the_gates_its_base_lists_as_authoritative_as_lab_and_the_rest_as_lab_shadow(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = origin
    listing = _commit(repo, {"delivery.yaml": LISTING})
    head = _commit(repo, {"src/a.py": "a = 3\n", "docs/c.md": "d\n"})
    names = graded(tmp_path, monkeypatch, repo, [("pull_request", head, listing)])
    assert names == {head: ["lab/lint", "lab-shadow/docs", "lab-shadow/required"]}


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_head_listing_a_gate_itself_and_a_dev_push_grade_post_every_check_as_lab_shadow(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, _ = origin
    listing = _commit(repo, {"delivery.yaml": LISTING, "src/a.py": "a = 3\n", "docs/c.md": "d\n"})
    names = graded(tmp_path, monkeypatch, repo, [("pull_request", listing, base), ("push", listing, base)])
    assert names == {listing: ["lab-shadow/lint", "lab-shadow/docs", "lab-shadow/required"] * 2}


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
    listed = replace(plan({"lint": (unit(1),)}), authoritative=frozenset({"lint"}))
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
def test_a_grade_state_stored_before_the_list_was_kept_loads_with_none_and_keeps_posting_shadow(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    key = scheduler.GRADES + run.grade(HEAD_A).run
    stored = json.loads(run.lab.client.get(key))
    del stored["authoritative"]
    run.lab.client.set(key, json.dumps(stored))
    run.lease()
    run.tick(1100)
    assert run.grade(HEAD_A).authoritative == []
    assert [shadow for *_, shadow in run.lab.poster.posts] == [True, True, True, True]


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_merge_group_grade_takes_the_queue_list_and_a_pull_request_or_hotfix_grade_the_lab_list() -> None:
    block = {"authoritative": ["lint"], "queue": {"full_suite_entries": 5, "authoritative": ["unit"]}}
    for tier in (Priority.HOTFIX, Priority.PULL_REQUEST):
        assert scheduler.authoritative_gates(block, tier) == frozenset({"lint"})
    assert scheduler.authoritative_gates(block, Priority.QUEUE) == frozenset({"unit"})
    unlisted = {"authoritative": ["lint"], "queue": {"full_suite_entries": 5}}
    assert scheduler.authoritative_gates(unlisted, Priority.QUEUE) == frozenset()
    assert scheduler.authoritative_gates({"authoritative": ["lint"]}, Priority.QUEUE) == frozenset()


@pytest.mark.feature("lab-authoritative-check-names")
def test_a_frame_keeps_the_authoritative_gates_of_every_grade_but_a_promotion_or_backfill_one() -> None:
    gates = {"lint": (unit(1),)}
    for tier in (Priority.HOTFIX, Priority.PULL_REQUEST, Priority.QUEUE):
        assert scheduler.frame(tier, TREE, BASE, gates, ("unit", "lint")).authoritative == frozenset({"lint", "unit"})
    for tier in (Priority.PROMOTION, Priority.BACKFILL):
        assert scheduler.frame(tier, TREE, BASE, gates, ("lint",)).authoritative == frozenset()


@pytest.mark.feature("lab-authoritative-check-names")
@pytest.mark.usefixtures("staticless")
def test_a_merge_group_grade_takes_no_gate_from_the_pull_request_list(tmp_path: Path) -> None:
    suite = _suite(tmp_path, {**DELIVERY["lab"], "authoritative": ["lint"]})
    expander = Port(SUITE_MANIFEST).expander(tmp_path)
    assert expander.expand(suite.request(Priority.PULL_REQUEST)).authoritative == frozenset({"lint"})
    assert expander.expand(suite.request(Priority.QUEUE)).authoritative == frozenset()
