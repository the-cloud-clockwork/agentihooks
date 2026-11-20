import ast
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.webhooks import HeadEvent
from tests.ci_lab.test_scheduler import (
    ACTOR,
    ALPHA,
    BASE,
    DELIVERY,
    HEAD_A,
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
    assert state.client.hgetall(scheduler.WAITING) == {}
    assert [grade.head for grade in run.grades()] == [suite.head]
