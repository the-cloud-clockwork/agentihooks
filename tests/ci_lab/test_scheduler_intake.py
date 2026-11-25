from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import bus
from tests.ci_lab.test_scheduler import Delivery
from tests.ci_lab.test_scheduler_shadow import completed_check


@dataclass
class Pages:
    remaining: list[bus.Delivery]
    asked: list[tuple[int, int]] = field(default_factory=list)
    step: float = 0.0
    now: float = 100.0
    fail_after: int | None = None

    def fetch(self, batch: int, timeout_ms: int) -> list[bus.Delivery]:
        if self.fail_after == len(self.asked):
            raise RuntimeError("later fetch failed")
        self.asked.append((batch, timeout_ms))
        self.now += self.step
        deliveries, self.remaining = self.remaining[:batch], self.remaining[batch:]
        return deliveries


def head(number: int) -> Delivery:
    return Delivery(
        envelope(HeadEvent("push", "the-cloud-clockwork/agentihooks", f"{number:040x}", "b" * 40, "developer", ""))
    )


def clock(monkeypatch: pytest.MonkeyPatch, pages: Pages) -> None:
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: pages.now))


@pytest.mark.feature("lab-intake-drain")
def test_one_intake_drains_a_backlog_larger_than_the_default_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    deliveries = [head(number) for number in range(101)]
    pages = Pages([*deliveries])
    clock(monkeypatch, pages)

    heads = scheduler.BusInbox(pages).heads()

    assert [item.event.head for item in heads] == [f"{number:040x}" for number in range(101)]
    assert pages.asked == [(50, 100)] * 3
    assert pages.remaining == []
    assert all(delivery.acked for delivery in deliveries)


@pytest.mark.feature("lab-intake-drain")
@pytest.mark.parametrize("count", [0, 1])
def test_a_short_or_empty_batch_ends_intake_without_another_fetch(count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = Pages([head(number) for number in range(count)])
    clock(monkeypatch, pages)

    assert len(scheduler.BusInbox(pages, batch=2, timeout_ms=17).heads()) == count
    assert pages.asked == [(2, 17)]


@pytest.mark.feature("lab-intake-drain")
def test_a_full_completed_check_batch_does_not_hide_the_head_in_the_next_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = [Delivery(completed_check("lint", "success")), Delivery(completed_check("types", "failure"))]
    last = head(1)
    pages = Pages([*checks, last])
    clock(monkeypatch, pages)
    seen: list[Mapping[str, Any]] = []

    heads = scheduler.BusInbox(pages, batch=2, observe=seen.append).heads()

    assert [item.event.head for item in heads] == [last.event.data["head"]]
    assert seen == [delivery.event.data for delivery in checks]
    assert all(delivery.acked for delivery in [*checks, last])
    assert pages.asked == [(2, 100)] * 2


@pytest.mark.feature("lab-intake-drain")
@pytest.mark.parametrize(("step", "count"), [(10.0, 2), (5.0, 4), (4.999, 5)])
def test_intake_stops_at_its_deadline_and_the_next_call_reads_the_remaining_heads(
    step: float, count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliveries = [head(number) for number in range(5)]
    pages = Pages([*deliveries], step=step)
    clock(monkeypatch, pages)
    inbox = scheduler.BusInbox(pages, batch=2)

    first = inbox.heads()

    assert [item.event.head for item in first] == [f"{number:040x}" for number in range(count)]
    assert [delivery.acked for delivery in deliveries] == [number < count for number in range(5)]
    pages.step = 0.0
    assert [item.event.head for item in inbox.heads()] == [f"{number:040x}" for number in range(count, 5)]


@pytest.mark.feature("lab-intake-drain")
@pytest.mark.parametrize("failure", ["fetch", "observer"])
def test_a_later_batch_failure_leaves_all_collected_heads_unacknowledged(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = head(1), head(2)
    check = Delivery(completed_check("lint", "success"))
    later = Delivery(completed_check("types", "failure"))
    pages = Pages([first, check, second, later], fail_after=1 if failure == "fetch" else None)
    clock(monkeypatch, pages)
    seen: list[Mapping[str, Any]] = []

    def observe(data: Mapping[str, Any]) -> None:
        assert not first.acked
        assert not second.acked
        if seen:
            raise RuntimeError("later observer failed")
        seen.append(data)

    with pytest.raises(RuntimeError, match=f"later {failure} failed"):
        scheduler.BusInbox(pages, batch=2, observe=observe).heads()

    assert not first.acked
    assert not second.acked
    assert check.acked
    assert not later.acked
    assert seen == [check.event.data]
