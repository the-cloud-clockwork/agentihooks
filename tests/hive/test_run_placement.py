from types import SimpleNamespace
from typing import cast

import pytest

from scripts.hive import run_placement
from scripts.hive.placement import Wait

pytestmark = pytest.mark.unit

NOW = 1_794_000_000_000
REPO = "/home/worker/dev/agentihooks"
RUN = run_placement.Run("delivery-1", "grade", "/cases/grade.json", REPO, "owner@fixture", ("build", "lint"))


def hive(hive_id: str, **changes: object) -> dict[str, object]:
    return {
        "id": hive_id,
        "repos": [REPO],
        "capabilities": ["build", "lint", "test"],
        "heartbeat_at": NOW,
        "beat_at": NOW,
        "draining": "no",
        **changes,
    }


class Orders:
    def __init__(self, running: dict[str, int]) -> None:
        self.counts = running
        self.sent: list[tuple[str, run_placement.Run]] = []

    def running(self, hive_id: str) -> int:
        return self.counts.get(hive_id, 0)

    def send(self, hive_id: str, run: run_placement.Run) -> str:
        self.sent.append((hive_id, run))
        return "1-0"


@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_hive_run_goes_to_the_fitting_hive_with_fewest_running_runs() -> None:
    hives = [hive("busy"), hive("calm"), hive("idle-b"), hive("idle-a")]
    running = {"busy": 3, "calm": 1}
    assert run_placement.choose(hives, REPO, ("build",), running, NOW) == "idle-a"
    assert run_placement.choose(hives[:2], REPO, ("build",), running, NOW) == "calm"


@pytest.mark.parametrize(
    "unfit",
    [
        {"heartbeat_at": NOW - 10 * 60_000, "beat_at": NOW - 10 * 60_000},
        {"draining": "yes"},
        {"repos": ["/home/worker/dev/agentibrain-kernel"]},
        {"capabilities": ["build"]},
    ],
)
@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_stale_draining_unhosting_or_unequipped_hive_is_never_chosen(unfit: dict[str, object]) -> None:
    hives = [hive("unfit", **unfit), hive("fit")]
    assert run_placement.choose(hives, REPO, RUN.needs, {"fit": 9}, NOW) == "fit"


@pytest.mark.feature("scheduled-run-hive-placement")
def test_no_live_hive_listing_the_repo_names_the_repo() -> None:
    hives = [hive("other", repos=["/home/worker/dev/agentibrain-kernel"]), hive("old", heartbeat_at=0)]
    assert run_placement.choose(hives, REPO, RUN.needs, {}, NOW) == Wait(f"no live hive lists the repo {REPO}")


@pytest.mark.feature("scheduled-run-hive-placement")
def test_no_hosting_hive_with_every_need_names_the_missing_needs() -> None:
    hives = [hive("half", capabilities=["build"]), hive("other", capabilities=["lint"])]
    assert run_placement.choose(hives, REPO, RUN.needs, {}, NOW) == Wait(
        f"no live hive that lists the repo {REPO} declares build, lint"
    )


@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_hive_declaring_exactly_the_needs_fits_and_one_declaring_none_does_not() -> None:
    hives = [hive("bare", capabilities=None), hive("exact", capabilities=["build", "lint"])]
    assert run_placement.choose(hives, REPO, RUN.needs, {"exact": 5}, NOW) == "exact"


@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_placed_run_carries_the_hive_path_of_its_repository() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    orders = Orders({})
    elsewhere = hive("far", repos=["/srv/hive/agentihooks"])
    assert run_placement.deliver(redis, RUN, [elsewhere], orders, NOW) == "far"
    assert [(hive_id, run.project) for hive_id, run in orders.sent] == [("far", "/srv/hive/agentihooks")]


@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_placed_run_sends_one_run_order_to_the_chosen_hive() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    orders = Orders({"a": 2})
    assert run_placement.deliver(redis, RUN, [hive("a"), hive("b")], orders, NOW) == "b"
    assert orders.sent == [("b", RUN)]
    assert run_placement.waits(redis) == {}


@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_run_with_no_fitting_hive_records_its_wait_and_sends_nothing() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    orders = Orders({})
    reason = f"no live hive that lists the repo {REPO} declares build, lint"
    assert run_placement.deliver(redis, RUN, [hive("a", capabilities=[])], orders, NOW) == Wait(reason)
    assert orders.sent == []
    assert run_placement.waits(redis) == {
        "delivery-1": {"workflow": "grade", "project": REPO, "reason": reason, "seen_at": NOW}
    }
    assert redis.hexists(run_placement.WAITS, "delivery-1")


@pytest.mark.feature("scheduled-run-hive-placement")
def test_a_later_placement_clears_the_recorded_wait() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    run_placement.deliver(redis, RUN, [], Orders({}), NOW)
    assert list(run_placement.waits(redis)) == ["delivery-1"]
    run_placement.deliver(redis, RUN, [hive("a")], Orders({}), NOW + 1)
    assert run_placement.waits(redis) == {}


@pytest.mark.feature("scheduled-run-hive-placement")
def test_dispatch_sends_a_run_order_on_the_chosen_hive_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    from scripts.hive import registry, run_orders

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(run_placement, "time", SimpleNamespace(time=lambda: NOW / 1000))
    stale = hive("a", heartbeat_at=NOW - 100_000, beat_at=NOW - 100_000)
    monkeypatch.setattr(registry, "hives", lambda client: [stale, hive("b"), hive("c")] if client is redis else [])
    store = run_orders.RunOrders(redis)
    first = run_placement.RunOrderSender(redis, NOW / 1000)
    first.send("c", run_placement.Run("other", "grade", "/c", REPO, "o", ()))
    assert first.running("c") == 1
    assert run_placement.dispatch(RUN, redis) == "b"
    [(_, fields)] = cast("list[tuple[str, dict[str, str]]]", redis.xrange(store.stream("b")))
    assert run_orders.RunOrder.decode(fields["order"]) == run_orders.RunOrder(
        "delivery-1", "grade", "/cases/grade.json", REPO, "owner@fixture", ("build", "lint"), "b", NOW // 1000 + 3600
    )
