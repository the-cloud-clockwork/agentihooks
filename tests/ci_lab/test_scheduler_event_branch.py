import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.digests import StaticInputs, Toolchain, static_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    HEAD_B,
    LOCK,
    REPO,
    ROOT,
    Consumer,
    Delivery,
    Run,
    _blob,
    _commit,
    _gate,
    _no_tests,
    _unknown,
    _without,
    plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler import origin as _origin

lab = _lab
origin = _origin

CHANGELOG = "lint/refuse-changelog-fragments-named-off-the-branch-or-opening-without-a-section-heading"
TRUSTED = "lint/require-trusted-actors-before-private-runner-admission"
SELF_PLANT = "lint/prove-the-mutation-gate-kills-with-the-real-test-and-fails-with-a-weakened-one"
ARTIFACT = "lint/artifact-sanity-checks"


@pytest.mark.feature("lab-lease-event-branch")
def test_a_grant_carries_the_event_of_its_grade_class_and_the_pull_request_head_branch(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    pulled = HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", "fix-x")
    run.lab.inbox.pending.append(scheduler.Head(pulled, HEAD_A))
    run.push(HEAD_B)
    run.tick(1000)
    grants = {grant.head: grant for grant in run.lease(slots=2)}
    assert (grants[HEAD_A].event, grants[HEAD_A].head_branch) == ("pull_request", "fix-x")
    assert (grants[HEAD_B].event, grants[HEAD_B].head_branch) == ("push", "")
    assert run.grade(HEAD_A).head_branch == "fix-x"


@pytest.mark.feature("lab-lease-event-branch")
def test_a_request_held_before_the_head_branch_field_grades_with_no_head_branch(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    tier = int(Priority.PULL_REQUEST)
    request = {"repo": REPO, "head": HEAD_A, "base": BASE, "tier": tier, "actor": ACTOR, "bypass_negative": False}
    held = {"request": request, "lane": HEAD_A, "ref": "", "since": 0}
    run.lab.client.hset(scheduler.WAITING, f"{REPO}|{HEAD_A}|{tier}", json.dumps(held))
    run.tick(1000)
    [grant] = run.lease()
    assert (grant.event, grant.head_branch) == ("pull_request", "")


@pytest.mark.feature("lab-lease-event-branch")
def test_the_bus_inbox_reads_the_head_branch_and_a_head_published_before_it_with_none() -> None:
    branched = HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", "fix-x")
    older = HeadEvent("pull_request", REPO, HEAD_B, BASE, ACTOR, "opened")
    deliveries = [Delivery(envelope(branched)), Delivery(_without(older, "head_branch"))]
    inbox = scheduler.BusInbox(Consumer([*deliveries]))
    assert inbox.heads() == [scheduler.Head(branched, HEAD_A), scheduler.Head(older, HEAD_B)]
    assert [delivery.acked for delivery in deliveries] == [True, True]


@pytest.mark.feature("lab-lease-event-branch")
def test_the_mirror_expander_keys_only_the_gates_that_read_an_event_name_with_its_value(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    plain = _gate("size", "lint", ("src",), ())
    evented = replace(_gate("plant", "lint", ("src",), ()), reads=("github.event_name",))
    branched = replace(_gate("fragments", "lint", ("src",), ()), reads=("github.head_ref", "github.ref_name"))

    def discover(workflow: str, registry: Any, reusable: Any) -> list[StaticGate]:
        return [plain, evented, branched]

    monkeypatch.setattr(scheduler, "discover", discover)
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    chain = Toolchain("3.12", {}, _blob(repo, head, LOCK), _blob(repo, head, "pyproject.toml"), "image", {})
    files = {"src/a.py": _blob(repo, head, "src/a.py")}

    def keyed(name: str, grader: str, event: dict[str, str]) -> Unit:
        return Unit("static", static_key(StaticInputs(name, files, {}, grader, chain, event)), (name,))

    request = GradeRequest(f"file://{repo}", head, base, Priority.PULL_REQUEST, ACTOR, False, "fix-x")
    assert expander.expand(request).gates["lint"] == (
        keyed("size", base, {}),
        keyed("plant", base, {"github.event_name": "pull_request"}),
        keyed("fragments", base, {"github.head_ref": "fix-x", "github.ref_name": ""}),
    )
    assert expander.expand(replace(request, head_branch="")).gates["lint"] == (
        keyed("size", base, {}),
        keyed("plant", base, {"github.event_name": "pull_request"}),
    )
    promoted = expander.expand(replace(request, tier=Priority.PROMOTION, head_branch=""))
    assert promoted.gates["lint"] == (keyed("size", head, {}), keyed("plant", head, {"github.event_name": "push"}))


@pytest.mark.feature("lab-lease-event-branch")
def test_the_mirror_expander_plans_the_changelog_fragment_gate_for_a_pull_request_head_with_its_branch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "origin"
    _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
    workflows = ROOT / ".github" / "workflows"
    tree = {f".github/workflows/{path.name}": path.read_text() for path in workflows.iterdir() if path.is_file()}
    tree |= {name: (ROOT / name).read_text() for name in (".github/input-keys.json", "delivery.yaml")}
    base = _commit(repo, {**tree, LOCK: "lock"})
    head = _commit(repo, {"src/a.py": "a = 1\n"})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    request = GradeRequest(f"file://{repo}", head, base, Priority.PULL_REQUEST, ACTOR, False, "fix-x")

    def linted(found: scheduler.Plan) -> list[str]:
        return [name for item in found.gates["lint"] for name in item.members]

    planned = linted(expander.expand(request))
    assert {CHANGELOG, TRUSTED, SELF_PLANT} <= set(planned)
    assert ARTIFACT not in planned
    assert CHANGELOG not in linted(expander.expand(replace(request, head_branch="")))
