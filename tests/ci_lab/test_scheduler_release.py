from collections.abc import Callable

import pytest

from scripts.ci_lab import control, manifests, scheduler
from scripts.ci_lab.intake import GradeRequest
from tests.ci_lab.test_scheduler import Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
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
def test_a_head_put_back_for_its_manifest_fetch_still_takes_one_of_the_eight_places(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, held: type[manifests.FetchUnanswered]
) -> None:
    run, heads = backlog(lab, 9)
    fetching = heads[:8]
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
    assert [request.head for request in run.lab.expander.seen] == fetching
    assert run.grades() == []
    assert run.lab.client.hlen(scheduler.WAITING) == 9
