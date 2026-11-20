from collections.abc import Callable

import pytest
import yaml

from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.manifests import FetchUnanswered
from tests.ci_lab.test_scheduler import BASE, HEAD_A, REPO, SLUG, Run
from tests.ci_lab.test_scheduler import lab as _lab

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
