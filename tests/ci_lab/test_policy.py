import pytest

from scripts.ci_lab import policy
from scripts.ci_lab.delivery import CLASSES


def work(
    key: str,
    branch: str = "busy",
    head: str = "head",
    priority: str = "pull_request",
    estimate: float = 1.0,
    state: str = "pending",
) -> policy.Pending:
    return policy.Pending(key, branch, head, priority, estimate, state)


def test_twenty_seven_heads_cannot_starve_another_branch() -> None:
    pending = [work(str(index), head=str(index)) for index in range(27)]
    neighbour = work("neighbour", branch="quiet")
    ordered = policy.order([*pending, neighbour])
    assert ordered[:2] == [pending[0], neighbour]
    assert ordered[2:] == pending[1:]


def test_priority_classes_precede_branch_fairness() -> None:
    pending = [work(priority, priority=priority) for priority in reversed(CLASSES)]
    assert [unit.priority for unit in policy.order(pending)] == list(CLASSES)


def test_longest_first_within_head_and_heads_keep_arrival_order() -> None:
    short = work("short", estimate=2)
    long = work("long", estimate=4)
    later = work("later", head="later", estimate=10)
    assert policy.order([short, later, long]) == [long, short, later]


def test_deficits_carry_between_rounds_and_charge_estimates() -> None:
    expensive = work("expensive", estimate=3)
    cheap = [work(str(index), branch="cheap") for index in range(4)]
    assert policy.order([expensive, *cheap]) == [*cheap[:2], expensive, *cheap[2:]]
    assert policy.order([expensive, *cheap], quantum=3) == [expensive, *cheap]


def test_empty_queue_and_large_estimate() -> None:
    assert policy.order([]) == []
    enormous = work("enormous", estimate=1_000_000_000)
    assert policy.order([enormous]) == [enormous]


@pytest.mark.parametrize("estimate", [0, -1, float("inf"), float("nan")])
def test_invalid_estimates_are_rejected(estimate: float) -> None:
    with pytest.raises(ValueError, match="^Deficit costs and quantum must be finite and positive$"):
        policy.order([work("bad", estimate=estimate)])


@pytest.mark.parametrize("quantum", [0, -1, float("inf"), float("nan")])
def test_invalid_quantum_is_rejected(quantum: float) -> None:
    with pytest.raises(ValueError, match="^Deficit costs and quantum must be finite and positive$"):
        policy.order([work("valid")], quantum=quantum)


def test_supersession_never_touches_leased_or_started_work() -> None:
    pending = work("pending", head="old")
    leased = work("leased", head="old", state="leased")
    running = work("running", head="old", state="running")
    done = work("done", head="old", state="done")
    current = work("current", head="new")
    old_head = [pending, leased, running, done, current]
    assert policy.supersede(old_head, "new") == [pending]
    assert old_head == [pending, leased, running, done, current]
    assert policy.supersede([pending], "old") == []
    assert policy.supersede([], "new") == []


def test_debounce_honours_seconds_and_ignores_repeat_head() -> None:
    debounce = policy.Debounce(quiet_window=5, clock=lambda: 100)
    assert not debounce.ready("branch")
    debounce.observe("branch", "old")
    assert not debounce.ready("branch", 104)
    assert debounce.ready("branch", 105)
    debounce.observe("branch", "old", 106)
    assert debounce.ready("branch", 106)
    debounce.observe("branch", "new", 106)
    assert not debounce.ready("branch", 110)
    assert debounce.ready("branch", 111)


def test_debounce_clock_and_branches_are_independent() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    debounce = policy.Debounce(quiet_window=2, clock=clock)
    debounce.observe("first", "one")
    now[0] = 1
    debounce.observe("second", "two")
    now[0] = 2
    assert debounce.ready("first")
    assert not debounce.ready("second")
    now[0] = 3
    assert debounce.ready("second")


def test_zero_window_and_explicit_zero_time() -> None:
    debounce = policy.Debounce(quiet_window=0, clock=lambda: 200)
    debounce.observe("branch", "head", 0)
    assert debounce.ready("branch", 0)


@pytest.mark.parametrize("quiet_window", [-1, float("inf"), float("nan")])
def test_invalid_quiet_window_is_rejected(quiet_window: float) -> None:
    with pytest.raises(ValueError, match="^The quiet window must be finite and nonnegative$"):
        policy.Debounce(quiet_window=quiet_window)


def test_order_rejects_unknown_priority_and_excludes_started_work() -> None:
    with pytest.raises(ValueError, match="Unknown priority class"):
        policy.order([work("unknown", priority="other")])
    pending = work("pending")
    assert policy.order([work("leased", state="leased"), pending]) == [pending]


def test_debounce_defaults_are_the_public_quiet_window_and_clock() -> None:
    from time import monotonic

    debounce = policy.Debounce()
    assert debounce.quiet_window == 30
    assert debounce.clock is monotonic


def test_carried_credit_skips_only_empty_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from math import ceil

    calls: list[float] = []

    def bounded_ceil(value: float) -> int:
        calls.append(value)
        assert len(calls) <= 12
        return ceil(value)

    monkeypatch.setattr(policy, "ceil", bounded_ceil)
    first = work("first", head="first", estimate=3)
    later = work("later", head="later", estimate=4)
    neighbour = work("neighbour", branch="other", estimate=5)
    assert policy.order([first, later, neighbour]) == [first, neighbour, later]


def test_nonunit_quantum_preserves_estimated_costs() -> None:
    expensive = work("expensive", estimate=3)
    cheap = [work(str(index), branch="cheap") for index in range(4)]
    assert policy.order([expensive, *cheap], quantum=2) == [*cheap[:2], expensive, *cheap[2:]]
