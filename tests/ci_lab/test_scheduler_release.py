from collections.abc import Callable

import pytest

from scripts.ci_lab import scheduler
from tests.ci_lab.test_scheduler import Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab


@pytest.mark.feature("lab-scheduler-release-bound")
def test_a_pass_starts_eight_waiting_heads_the_most_urgent_class_first_and_admits_their_units_in_the_same_pass(
    lab: Callable[..., Run],
) -> None:
    heads = [f"{number:040x}" for number in range(1, 25)]
    run = lab({head: plan({"lint": (unit(number),)}, head) for number, head in enumerate(heads, 1)}, quiet_ms=500)
    by_lane = list(reversed(heads))
    for number, head in enumerate(by_lane):
        run.push(head, lane=f"lane-{number:02}", event=("push", "merge_group", "pull_request")[number % 3])
    run.tick(1000)
    actions = run.tick(1500)
    order = by_lane[2::3] + by_lane[1::3] + by_lane[::3]
    assert [request.head for request in run.lab.expander.seen] == order[:8]
    enqueued = sorted(action for action in actions if action.startswith("enqueued "))
    assert enqueued == sorted(f"enqueued 1 units of {run.grade(head).run}" for head in order[:8])
    assert len(run.lab.client.hgetall(scheduler.WAITING)) == 16
    run.tick(1502)
    run.tick(1504)
    assert [request.head for request in run.lab.expander.seen] == order
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


@pytest.mark.feature("lab-scheduler-release-bound")
def test_the_eight_heads_a_pass_starts_are_the_longest_waiting_and_a_quiet_head_takes_no_place(
    lab: Callable[..., Run],
) -> None:
    heads = [f"{number:040x}" for number in range(1, 25)]
    run = lab({head: plan({"lint": (unit(number),)}, head) for number, head in enumerate(heads, 1)}, quiet_ms=500)
    older, newer, quiet = heads[:8], heads[8:16], heads[16:]
    for at, lane, event, group in (
        (1000, "z", "merge_group", older),
        (1100, "a", "merge_group", newer),
        (1400, "q", "pull_request", quiet),
    ):
        for number, head in enumerate(group):
            run.push(head, lane=f"{lane}-{number}", event=event)
        run.tick(at)
    run.tick(1600)
    assert sorted(request.head for request in run.lab.expander.seen) == older
    assert len(run.lab.client.hgetall(scheduler.WAITING)) == 16
