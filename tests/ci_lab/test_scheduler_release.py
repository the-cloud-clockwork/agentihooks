import json
import subprocess
from collections.abc import Callable

import pytest
import requests

from scripts.ci_lab import checks, control, manifests, scheduler
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.retry import Policy
from tests.ci_lab.test_scheduler import (
    CHECK_RUNS,
    HEAD_A,
    HEAD_B,
    SLUG,
    FixtureToken,
    Run,
    github,
    plan,
    pull_plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler_uncarried import REASON, reaching

lab = _lab
GIT_FAILURE = subprocess.CalledProcessError(128, ["git", "ls-remote"])


def _unreachable(repo: str, head: str) -> bool:
    raise GIT_FAILURE


EVENTS = {
    "backfill": "backfill",
    "promotion": "push",
    "queue": "merge_group",
    "pull_request": "pull_request",
    "hotfix": "pull_request",
}


def backlog(lab: Callable[..., Run], count: int) -> tuple[Run, list[str]]:
    heads = [f"{number:040x}" for number in range(1, count + 1)]
    plans = {head: plan({"lint": (unit(number),)}, head) for number, head in enumerate(heads, 1)}
    return lab(plans, quiet_ms=500), heads


@pytest.mark.feature("lab-scheduler-release-bound")
def test_each_pass_starts_eight_waiting_heads_in_class_order_and_admits_their_units_in_the_same_pass(
    lab: Callable[..., Run],
) -> None:
    run, heads = backlog(lab, 40)
    names = list(EVENTS)
    classes: dict[str, list[str]] = {name: [] for name in names}
    for number, head in enumerate(reversed(heads)):
        name = names[number % len(names)]
        classes[name].append(head)
        run.push(head, lane=f"lane-{number:02}", event=EVENTS[name])
    run.lab.fixes.update(classes["hotfix"])
    order = [head for name in reversed(EVENTS) for head in classes[name]]
    run.tick(1000)
    assert [request.head for request in run.lab.expander.seen] == order[:8]
    actions = run.tick(1500)
    assert [request.head for request in run.lab.expander.seen] == order[:16]
    enqueued = sorted(action for action in actions if action.startswith("enqueued "))
    assert enqueued == sorted(f"enqueued 1 units of {run.grade(head).run}" for head in order[8:16])
    assert run.lab.client.hlen(scheduler.WAITING) == 24
    for at in (1502, 1504, 1506):
        run.tick(at)
    assert [request.head for request in run.lab.expander.seen] == order
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-scheduler-release-bound")
def test_the_eight_heads_a_pass_starts_are_the_longest_waiting_and_quiet_or_paused_heads_take_no_place(
    lab: Callable[..., Run],
) -> None:
    run, heads = backlog(lab, 32)
    older, newer, quiet, paused = heads[:8], heads[8:16], heads[16:24], heads[24:]
    run.lab.fixes.update(paused)
    control.pause(run.lab.client, "hotfix", [])
    for at, lane, event, group in (
        (1000, "z", "merge_group", older),
        (1100, "a", "merge_group", newer),
        (1400, "q", "pull_request", quiet),
        (1400, "h", "pull_request", paused),
    ):
        for number, head in enumerate(group):
            run.push(head, lane=f"{lane}-{number}", event=event)
        run.tick(at)
    run.tick(1600)
    assert sorted(request.head for request in run.lab.expander.seen) == older
    assert run.lab.client.hlen(scheduler.WAITING) == 24


@pytest.mark.feature("lab-scheduler-release-bound")
@pytest.mark.parametrize("held", [manifests.FetchPending, manifests.FetchUnanswered])
def test_a_head_put_back_for_its_manifest_fetch_takes_no_place_and_the_pass_moves_on_to_later_heads(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, held: type[manifests.FetchUnanswered]
) -> None:
    run, heads = backlog(lab, 9)
    fetching = heads[:8]
    starting = heads[8]
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        found = planned(request)
        if request.head in fetching:
            raise held(f"manifest of {request.base} is still fetching")
        return found

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    for number, head in enumerate(heads):
        run.push(head, lane=f"lane-{number}", event="merge_group")
        run.tick(1000 + number)
    run.tick(1600)
    assert [request.head for request in run.lab.expander.seen] == heads
    assert [grade.head for grade in run.grades()] == [starting]
    assert run.lab.client.hlen(scheduler.WAITING) == 8


@pytest.mark.feature("lab-scheduler-release-bound")
@pytest.mark.parametrize("held", [manifests.FetchPending, manifests.FetchUnanswered])
def test_eight_heads_put_back_for_their_manifest_fetch_do_not_use_up_the_eight_starts_the_eight_heads_behind_them_take(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, held: type[manifests.FetchUnanswered]
) -> None:
    run, heads = backlog(lab, 16)
    fetching, starting = heads[:8], heads[8:]
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        found = planned(request)
        if request.head in fetching:
            raise held(f"manifest of {request.base} is still fetching")
        return found

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    for number, head in enumerate(heads):
        run.push(head, lane=f"lane-{number}", event="merge_group")
        run.tick(1000 + number)
    run.tick(1700)
    assert [request.head for request in run.lab.expander.seen] == heads
    assert sorted(grade.head for grade in run.grades()) == starting
    assert run.lab.client.hlen(scheduler.WAITING) == 8


@pytest.mark.feature("lab-scheduler-release-bound")
@pytest.mark.parametrize("held", [manifests.FetchPending, manifests.FetchUnanswered])
def test_more_than_twenty_four_heads_all_put_back_for_their_manifest_fetch_one_pass_looks_at_exactly_twenty_four(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, held: type[manifests.FetchUnanswered]
) -> None:
    run, heads = backlog(lab, 30)
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        planned(request)
        raise held(f"manifest of {request.base} is still fetching")

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    for number, head in enumerate(heads):
        run.push(head, lane=f"lane-{number}", event="merge_group")
        run.tick(1000 + number)
    run.tick(1700)
    assert [request.head for request in run.lab.expander.seen] == heads[:24]
    assert run.grades() == []
    assert run.lab.client.hlen(scheduler.WAITING) == 30


@pytest.mark.feature("lab-scheduler-release-bound")
@pytest.mark.parametrize("held", [manifests.FetchPending, manifests.FetchUnanswered])
def test_a_head_put_back_for_its_manifest_fetch_keeps_its_waiting_record(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, held: type[manifests.FetchUnanswered]
) -> None:
    run, heads = backlog(lab, 1)
    head = heads[0]
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        planned(request)
        raise held(f"manifest of {request.base} is still fetching")

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(head, lane="lane-0", event="merge_group")
    run.tick(1000)
    before = run.lab.client.hgetall(scheduler.WAITING)
    run.tick(1600)
    raw = next(iter(run.lab.client.hgetall(scheduler.WAITING).values()))
    record = json.loads(raw)
    assert run.lab.client.hlen(scheduler.WAITING) == 1
    assert "starts" not in record
    if held is manifests.FetchPending:
        assert run.lab.client.hgetall(scheduler.WAITING) == before
        assert record["since"] == 1000
        assert "unanswered" not in record
    else:
        assert record["since"] == 1600
        assert record["unanswered"] == 1


@pytest.mark.feature("lab-scheduler-release-bound")
def test_eight_heads_that_cannot_expand_are_dropped_and_take_eight_of_the_starts_places(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, heads = backlog(lab, 16)
    failing, waiting = heads[:8], heads[8:]
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        found = planned(request)
        if request.head in failing:
            raise ValueError(f"no dependency manifest at or before {request.base}")
        return found

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    for number, head in enumerate(heads):
        run.push(head, lane=f"lane-{number:02}", event="merge_group")
        run.tick(1000 + number)
    run.tick(1700)
    assert [request.head for request in run.lab.expander.seen] == failing
    assert run.grades() == []
    assert run.lab.client.hlen(scheduler.WAITING) == len(waiting)


@pytest.mark.feature("lab-scheduler-release-bound")
def test_eight_heads_whose_tenth_unanswered_fetch_arrives_in_the_same_pass_take_the_eight_places(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, heads = backlog(lab, 16)
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        planned(request)
        raise manifests.FetchUnanswered(f"GET actions/workflows/test.yml/runs answered 502 for {request.base}")

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    for number, head in enumerate(heads):
        run.push(head, lane=f"lane-{number:02}", event="merge_group")
        run.tick(1000 + number)
    at = 1700
    for _ in range(9):
        run.tick(at)
        at += 600
    records = [json.loads(raw) for raw in run.lab.client.hvals(scheduler.WAITING)]
    assert {record["unanswered"] for record in records} == {9}
    assert run.lab.client.hlen(scheduler.WAITING) == 16
    seen_before = len(run.lab.expander.seen)
    run.tick(at)
    assert len(run.lab.expander.seen) - seen_before == 8
    assert run.lab.client.hlen(scheduler.WAITING) == 8
    records = [json.loads(raw) for raw in run.lab.client.hvals(scheduler.WAITING)]
    assert {record["unanswered"] for record in records} == {9}
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-release-keeps-a-cut-short-head")
def test_a_head_cut_short_by_a_stop_during_its_expansion_is_started_by_the_next_scheduler_and_graded(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, heads = backlog(lab, 1)
    head = heads[0]
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        if not run.lab.expander.seen:
            run.lab.expander.seen.append(request)
            raise KeyboardInterrupt
        return planned(request)

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(head, lane="lane-0", event="merge_group")
    run.tick(1000)
    with pytest.raises(KeyboardInterrupt):
        run.tick(1600)
    assert run.lab.client.hlen(scheduler.WAITING) == 1
    raw = next(iter(run.lab.client.hgetall(scheduler.WAITING).values()))
    assert json.loads(raw)["starts"] == 1
    again = Run(run.lab, run.lab.build(500, Policy(), None))
    again.tick(2200)
    assert again.grade(head).run in again.lab.client.smembers(scheduler.LIVE)
    assert again.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-scheduler-release-keeps-a-cut-short-head")
def test_five_starts_in_a_row_cut_short_by_a_stop_count_up_and_the_sixth_pass_refuses_the_head(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, heads = backlog(lab, 1)
    head = heads[0]

    def expand(request: GradeRequest) -> scheduler.Plan:
        raise KeyboardInterrupt

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(head, lane="lane-0", event="merge_group")
    at = 1000
    run.tick(at)
    for count in range(1, scheduler.CUT_SHORT_STARTS + 1):
        at += 600
        with pytest.raises(KeyboardInterrupt):
            run.tick(at)
        assert run.lab.client.hlen(scheduler.WAITING) == 1
        raw = next(iter(run.lab.client.hgetall(scheduler.WAITING).values()))
        assert json.loads(raw)["starts"] == count
    at += 600
    actions = run.tick(at)
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert any(f"{scheduler.CUT_SHORT_STARTS} starts" in action for action in actions)
    assert run.lab.poster.moves(head, scheduler.REQUIRED) == [("completed", "failure")]


@pytest.mark.feature("lab-scheduler-release-keeps-a-cut-short-head")
def test_an_expander_error_the_release_loop_does_not_name_still_removes_the_waiting_field_before_it_leaves_the_pass(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run, heads = backlog(lab, 1)
    head = heads[0]

    def expand(request: GradeRequest) -> scheduler.Plan:
        raise OSError("no space left on device")

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(head, lane="lane-0", event="merge_group")
    run.tick(1000)
    with pytest.raises(OSError):
        run.tick(1600)
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-scheduler-release-keeps-a-cut-short-head")
def test_a_cut_short_refusal_counts_toward_the_pass_s_starts_cap_so_a_later_head_waits(
    lab: Callable[..., Run],
) -> None:
    run, heads = backlog(lab, 9)
    cut_short, rest = heads[0], heads[1:]
    run.push(cut_short, lane="0cut", event="merge_group")
    for number, head in enumerate(rest):
        run.push(head, lane=f"lane-{number:02}", event="merge_group")
    run.tick(1000)
    name, raw = next(
        (name, raw)
        for name, raw in run.lab.client.hgetall(scheduler.WAITING).items()
        if json.loads(raw)["request"]["head"] == cut_short
    )
    record = json.loads(raw)
    run.lab.client.hset(scheduler.WAITING, name, json.dumps({**record, "starts": scheduler.CUT_SHORT_STARTS}))
    run.tick(1600)
    assert len(run.lab.expander.seen) == 7
    assert run.lab.client.hlen(scheduler.WAITING) == 1


def _real_checks(run: Run) -> tuple[checks.Checks, Run]:
    poster = checks.Checks(SLUG, FixtureToken())
    ports = scheduler.Ports(run.lab.inbox, run.lab.expander, lambda _repo: poster, run.lab.hotfixes, run.lab.kube)
    engine = scheduler.Scheduler(run.lab.client, run.lab.objects, ports, run.engine.settings)
    return poster, Run(run.lab, engine)


@pytest.mark.feature("lab-scheduler-fail-skips-a-second-completed-post")
def test_a_head_refused_from_two_waiting_entries_in_one_pass_posts_its_failed_check_once(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(requests, "request", github(calls))
    monkeypatch.setattr(run.lab.expander, "reachable", _unreachable)
    poster, real = _real_checks(run)
    real.push(HEAD_A, lane="own-sha")
    real.push(HEAD_A, lane="engineer-branch")
    real.push(HEAD_B, lane="other")
    actions = real.tick(1000)
    assert actions.count(f"cannot expand {HEAD_A}: {GIT_FAILURE}") == 2
    assert f"cannot expand {HEAD_B}: {GIT_FAILURE}" in actions
    assert poster.status((HEAD_A, checks.name(scheduler.REQUIRED, True), "unexpanded-1000")) == "completed"
    assert calls.count(("POST", CHECK_RUNS)) == 2
    assert real.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-scheduler-fail-skips-a-second-completed-post")
def test_a_pull_request_head_retired_from_two_waiting_entries_in_one_pass_posts_its_failed_check_once(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(requests, "request", github(calls))
    reaching(run, monkeypatch, HEAD_A)
    poster, real = _real_checks(run)
    real.push(HEAD_A, event="pull_request", action="synchronize", lane="own-sha")
    real.push(HEAD_A, event="pull_request", action="synchronize", lane="engineer-branch")
    actions = real.tick(1000)
    retiring = f"retiring {HEAD_A}: {REASON}"
    skipped = f"the failed {scheduler.REQUIRED} check of {HEAD_A} was already posted in this pass"
    assert [action for action in actions if action.startswith("retiring")] == [retiring, f"{retiring}; {skipped}"]
    assert calls.count(("POST", CHECK_RUNS)) == 1
    assert real.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-scheduler-fail-skips-a-second-completed-post")
def test_a_head_refused_in_two_different_passes_still_posts_a_failed_check_in_each(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(requests, "request", github(calls))
    monkeypatch.setattr(run.lab.expander, "reachable", _unreachable)
    poster, real = _real_checks(run)
    real.push(HEAD_A, lane="first")
    first = real.tick(1000)
    real.push(HEAD_A, lane="first")
    second = real.tick(2000)
    assert first == [f"received {HEAD_A} on first", f"cannot expand {HEAD_A}: {GIT_FAILURE}"]
    assert second == [f"received {HEAD_A} on first", f"cannot expand {HEAD_A}: {GIT_FAILURE}"]
    assert calls.count(("POST", CHECK_RUNS)) == 2
    assert poster.status((HEAD_A, checks.name(scheduler.REQUIRED, True), "unexpanded-1000")) == "completed"
    assert poster.status((HEAD_A, checks.name(scheduler.REQUIRED, True), "unexpanded-2000")) == "completed"
