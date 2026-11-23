import ast
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.manifests import FetchPending, FetchUnanswered
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.webhooks import HeadEvent
from tests.ci_lab.test_scheduler import (
    ACTOR,
    ALPHA,
    BASE,
    DELIVERY,
    HEAD_A,
    HEAD_B,
    REPO,
    SLUG,
    SUITE_MANIFEST,
    Expander,
    Inbox,
    Lab,
    Port,
    Run,
    _commit,
    _suite,
    fake_redis,
    plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler import staticless as staticless

lab = _lab


@pytest.mark.feature("lab-scheduler-unexpanded-head")
def test_a_head_that_cannot_expand_posts_a_failed_required_check_naming_its_reason(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})

    def malformed(request: GradeRequest) -> scheduler.Plan:
        raise yaml.YAMLError(f"malformed workflow at {request.base}")

    monkeypatch.setattr(run.lab.expander, "expand", malformed)
    run.push(HEAD_A)
    assert run.tick(1000)[-1] == f"cannot expand {HEAD_A}: malformed workflow at {BASE}"
    reason = f"cannot expand {HEAD_A} against {BASE}: malformed workflow at {BASE}"
    output = Output(
        "promotion grade cannot expand", reason, (), "unexpanded-1000", "unknown", f"{REPO}/commit/{HEAD_A}"
    )
    assert run.lab.poster.posts == [(HEAD_A, scheduler.REQUIRED, "completed", "failure", output, True)]
    assert run.lab.repos == [SLUG]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-unexpanded-head")
def test_a_grade_ref_head_that_cannot_expand_posts_no_check_run(lab: Callable[..., Run]) -> None:
    run = lab({})
    run.push(HEAD_A, lane="engineer-1", ref="refs/grade/t1")
    assert run.tick(1000)[-1].startswith(f"cannot expand {HEAD_A}: ")
    assert run.lab.poster.posts == []
    assert run.lab.repos == []
    assert run.grades() == []


@pytest.mark.feature("lab-unparsable-head-dropped")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_whose_changed_python_does_not_parse_is_dropped_and_the_pass_starts_the_next_head(
    tmp_path: Path,
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    source = ALPHA.replace("return 2", "return (2")
    broken = _commit(suite.repo, {"scripts/alpha.py": source})
    with pytest.raises(SyntaxError) as unparsable:
        ast.parse(source.encode())
    state = Lab(fake_redis(), LocalObjects(tmp_path / "objects"), Inbox(), Expander({}))
    expander = Port(SUITE_MANIFEST).expander(tmp_path)
    ports = scheduler.Ports(state.inbox, expander, state.checks, state.hotfixes, state.kube)
    settings = scheduler.Settings(frozenset({ACTOR}), 0, Policy())
    run = Run(state, scheduler.Scheduler(state.client, state.objects, ports, settings))
    for lane, head in (("a", broken), ("b", suite.head)):
        event = HeadEvent("push", suite.remote, head, suite.base, ACTOR, "pushed")
        state.inbox.pending.append(scheduler.Head(event, lane, ""))
    actions = run.tick(1000)
    assert f"cannot expand {broken}: {unparsable.value}" in actions
    reason = f"cannot expand {broken} against {suite.base}: {unparsable.value}"
    commit = f"{suite.remote}/commit/{broken}"
    output = Output("promotion grade cannot expand", reason, (), "unexpanded-1000", "unknown", commit)
    refusals = [post for post in state.poster.posts if post[0] == broken]
    assert refusals == [(broken, scheduler.REQUIRED, "completed", "failure", output, True)]
    assert state.client.hgetall(scheduler.WAITING) == {}
    assert [grade.head for grade in run.grades()] == [suite.head]


@pytest.mark.feature("lab-manifest-fetch-hold-cap")
def test_a_held_head_whose_manifest_fetch_gets_ten_server_errors_in_a_row_posts_a_failed_check_naming_them(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})

    def unanswered(request: GradeRequest) -> scheduler.Plan:
        raise FetchUnanswered(f"GET actions/workflows/test.yml/runs answered 502 for {request.base}")

    monkeypatch.setattr(run.lab.expander, "expand", unanswered)
    run.push(HEAD_A)
    error = f"GET actions/workflows/test.yml/runs answered 502 for {BASE}"
    for at in range(1000, 1009):
        assert run.tick(at)[-1] == f"holding {HEAD_A}: {error}"
    assert run.lab.poster.posts == []
    fetches = f"10 manifest fetches in a row got no answer or a server error, the last: {error}"
    assert run.tick(1009)[-1] == f"cannot expand {HEAD_A}: {fetches}"
    reason = f"cannot expand {HEAD_A} against {BASE}: {fetches}"
    output = Output(
        "promotion grade cannot expand", reason, (), "unexpanded-1009", "unknown", f"{REPO}/commit/{HEAD_A}"
    )
    assert run.lab.poster.posts == [(HEAD_A, scheduler.REQUIRED, "completed", "failure", output, True)]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


@pytest.mark.feature("lab-manifest-fetch-hold-cap")
def test_a_held_heads_unanswered_fetches_count_in_a_row_until_a_new_head_or_a_start_clears_them(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_B: plan({"lint": (unit(1),)})})
    expand = run.lab.expander.expand
    outcomes = iter([FetchUnanswered("502"), FetchPending("running"), FetchUnanswered("502"), FetchUnanswered("502")])

    def scripted(request: GradeRequest) -> scheduler.Plan:
        outcome = next(outcomes, None)
        if outcome is None:
            return expand(request)
        raise outcome

    def counts() -> dict[str, int]:
        held = [json.loads(raw) for raw in run.lab.client.hvals(scheduler.WAITING)]
        return {entry["request"]["head"]: entry.get("unanswered", 0) for entry in held}

    monkeypatch.setattr(run.lab.expander, "expand", scripted)
    run.push(HEAD_A)
    assert run.tick(1000)[-1] == f"holding {HEAD_A}: 502"
    assert counts() == {HEAD_A: 1}
    assert run.tick(1001)[-1] == f"holding {HEAD_A}: running"
    assert counts() == {HEAD_A: 1}
    run.push(HEAD_A)
    assert run.tick(1002)[-1] == f"holding {HEAD_A}: 502"
    assert counts() == {HEAD_A: 2}
    run.push(HEAD_B)
    assert run.tick(1003)[-1] == f"holding {HEAD_B}: 502"
    assert counts() == {HEAD_B: 1}
    run.tick(1004)
    assert counts() == {}
    assert [grade.head for grade in run.grades()] == [HEAD_B]
