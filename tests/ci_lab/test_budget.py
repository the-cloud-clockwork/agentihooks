import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from scripts.ci_lab import budget, capacity
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.delivery import runtime

RUNTIME = {
    "classes": ["hotfix", "pull_request", "queue", "promotion", "backfill"],
    "budget": {"coreMinutesPerDay": 1440, "shed": {"backfill": 0.6, "promotion": 0.8, "queue": 0.95}},
    "backfill": {"auditShare": 0.05, "moduleSweeps": True},
}
POLICY = budget.Policy(1440, {"backfill": 0.6, "promotion": 0.8, "queue": 0.95})
EVERY_SHEDDABLE = ("backfill", "promotion", "queue")


def at(day: int, hour: int, minute: int = 0, second: int = 0, ms: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, second, tzinfo=UTC).timestamp() * 1000) + ms


class Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


@pytest.fixture
def client() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def clock() -> Clock:
    return Clock(at(15, 12))


@pytest.fixture
def make(client: Any, clock: Clock) -> Callable[[budget.Policy], budget.Budget]:
    def build(policy: budget.Policy) -> budget.Budget:
        return budget.Budget(client, clock, policy)

    return build


def test_policy_reads_the_budget_of_the_validated_runtime_config() -> None:
    assert budget.policy(runtime(RUNTIME)) == POLICY


@pytest.mark.parametrize(
    ("now_ms", "key"),
    [
        (at(15, 0), "agentihooks-ci:budget:2026-11-15"),
        (at(15, 12), "agentihooks-ci:budget:2026-11-15"),
        (at(15, 23, 59, 59, 999), "agentihooks-ci:budget:2026-11-15"),
        (at(16, 0), "agentihooks-ci:budget:2026-11-16"),
    ],
)
def test_the_day_key_names_the_utc_date_of_the_instant(now_ms: int, key: str) -> None:
    assert budget.day_key(now_ms) == key


def test_the_day_key_ignores_the_host_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        assert budget.day_key(at(15, 10)) == "agentihooks-ci:budget:2026-11-15"
        assert budget.day_key(at(15, 23, 59, 59, 999)) == "agentihooks-ci:budget:2026-11-15"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_nothing_is_spent_before_the_first_charge(make: Callable[[budget.Policy], budget.Budget]) -> None:
    ledger = make(POLICY)

    assert ledger.spent() == 0.0
    assert ledger.status() == budget.Status(1.0, ())


def test_a_charge_adds_slots_times_seconds_as_core_minutes_to_the_days_counter(
    client: Any, make: Callable[[budget.Policy], budget.Budget]
) -> None:
    ledger = make(POLICY)

    first = ledger.charge(4, 90.0)
    second = ledger.charge(2, 15.0)

    assert first.fraction == pytest.approx(1 - 6 / 1440)
    assert first.shed == ()
    assert ledger.spent() == pytest.approx(6.5)
    assert second == ledger.status()
    assert float(client.get("agentihooks-ci:budget:2026-11-15")) == pytest.approx(6.5)
    assert client.ttl("agentihooks-ci:budget:2026-11-15") == budget.TTL_S == 172800


def test_the_counter_rolls_over_at_utc_midnight(make: Callable[[budget.Policy], budget.Budget], clock: Clock) -> None:
    ledger = make(POLICY)
    clock.now_ms = at(15, 23, 59, 59, 999)
    ledger.charge(1, 0.9 * 1440 * 60)

    assert ledger.status().shed == ("backfill", "promotion")

    clock.now_ms = at(16, 0)

    assert ledger.spent() == 0.0
    assert ledger.status() == budget.Status(1.0, ())


def test_a_forced_low_budget_sheds_backfill_first_and_never_pull_request_units(
    make: Callable[[budget.Policy], budget.Budget],
) -> None:
    ledger = make(POLICY)

    status = ledger.charge(1, 0.65 * 1440 * 60)

    assert status.shed == ("backfill",)
    assert status.fraction == pytest.approx(0.35)
    assert not budget.admits("backfill", 1, 8, 0, status.shed)
    assert budget.admits("promotion", 1, 8, 0, status.shed)
    assert budget.admits("pull_request", 1, 8, 0, status.shed)
    assert budget.admits("hotfix", 1, 8, 0, status.shed)
    load = budget.observe(Load({"backfill": (30.0,)}, {}, slots_total=8, slots_busy=2), status)
    shown = capacity.document(load)
    assert shown["shed"] == ["backfill"]
    assert shown["budget_fraction"] == status.fraction


def test_an_overspent_day_sheds_every_sheddable_class_and_still_admits_pull_request_and_hotfix_units(
    make: Callable[[budget.Policy], budget.Budget],
) -> None:
    ledger = make(POLICY)

    status = ledger.charge(16, 10 * 1440 * 60)

    assert status == budget.Status(0.0, EVERY_SHEDDABLE)
    assert [name for name in ("queue", "promotion", "backfill") if budget.admits(name, 1, 8, 0, status.shed)] == []
    assert budget.admits("pull_request", 1, 8, 0, status.shed)
    assert budget.admits("hotfix", 1, 8, 0, status.shed)


@pytest.mark.parametrize(
    ("share", "shed"),
    [
        (0.0, ()),
        (0.59, ()),
        (0.6, ("backfill",)),
        (0.79, ("backfill",)),
        (0.8, ("backfill", "promotion")),
        (0.949, ("backfill", "promotion")),
        (0.95, EVERY_SHEDDABLE),
        (3.0, EVERY_SHEDDABLE),
    ],
)
def test_classes_shed_in_order_once_the_spent_share_reaches_their_fraction(share: float, shed: tuple[str, ...]) -> None:
    assert budget.shed(share, POLICY.fractions) == shed


@pytest.mark.parametrize(
    ("share", "fractions", "shed"),
    [
        (0.6, {"backfill": 0.9, "promotion": 0.5}, ("backfill", "promotion")),
        (0.5, {"queue": 0.7}, ()),
        (0.7, {"queue": 0.7}, EVERY_SHEDDABLE),
        (0.0, {"backfill": 0.0}, ("backfill",)),
        (100.0, {}, ()),
    ],
)
def test_shedding_a_later_class_sheds_every_class_before_it(
    share: float, fractions: dict[str, float], shed: tuple[str, ...]
) -> None:
    assert budget.shed(share, fractions) == shed


@pytest.mark.parametrize(
    ("spent", "fraction"),
    [(0.0, 1.0), (360.0, 0.75), (1440.0, 0.0), (2880.0, 0.0)],
)
def test_the_fraction_left_falls_with_spending_and_never_below_zero(spent: float, fraction: float) -> None:
    assert budget.assess(spent, budget.Policy(1440, {})) == budget.Status(fraction, ())


def test_waiting_counts_pending_slots_of_every_class_but_backfill_that_may_lease() -> None:
    pending = {"hotfix": 1, "pull_request": 2, "queue": 3, "promotion": 4, "backfill": 5, "other": 6}

    assert budget.waiting(pending, ()) == 10
    assert budget.waiting(pending, ("backfill", "promotion")) == 6
    assert budget.waiting({}, ()) == 0


def test_backfill_never_raises_slots_wanted() -> None:
    running = {"pull_request": 2, "queue": 1, "backfill": 3}
    pending = {"pull_request": 4, "promotion": 1}

    wanted = budget.slots_wanted(running, pending, ())

    assert wanted == 8
    assert budget.slots_wanted(running, {**pending, "backfill": 40}, ()) == wanted
    assert budget.slots_wanted({**running, "backfill": 30}, pending, ()) == wanted


def test_a_shed_class_keeps_its_running_slots_wanted_but_not_its_pending_ones() -> None:
    running = {"queue": 2, "hotfix": 1}
    pending = {"queue": 5, "pull_request": 1}

    assert budget.slots_wanted(running, pending, ()) == 9
    assert budget.slots_wanted(running, pending, EVERY_SHEDDABLE) == 4


@pytest.mark.parametrize(
    ("slots", "free", "waiting", "admitted"),
    [(2, 5, 3, True), (2, 5, 4, False), (1, 1, 0, True), (1, 0, 0, False), (1, 8, 9, False)],
)
def test_backfill_leases_only_into_slots_left_free_after_waiting_units(
    slots: int, free: int, waiting: int, admitted: bool
) -> None:
    assert budget.admits("backfill", slots, free, waiting, ()) is admitted


@pytest.mark.parametrize("name", ["hotfix", "pull_request", "queue", "promotion"])
def test_other_classes_are_not_held_back_by_waiting_units(name: str) -> None:
    assert budget.admits(name, 4, 1, 10, ())


@pytest.mark.parametrize("name", EVERY_SHEDDABLE)
def test_a_shed_class_never_leases(name: str) -> None:
    assert not budget.admits(name, 1, 10, 0, (name,))


def test_the_status_fills_the_capacity_fraction_and_shed_classes() -> None:
    load = Load({"queue": (60.0, 30.0)}, {"queue": 1}, slots_total=4, slots_busy=1)

    observed = budget.observe(load, budget.Status(0.25, ("backfill", "promotion")))

    assert observed == Load(
        {"queue": (60.0, 30.0)},
        {"queue": 1},
        slots_total=4,
        slots_busy=1,
        budget_fraction=0.25,
        shed=("backfill", "promotion"),
    )
    shown = capacity.document(observed)
    assert shown["budget_fraction"] == 0.25
    assert shown["shed"] == ["promotion", "backfill"]
