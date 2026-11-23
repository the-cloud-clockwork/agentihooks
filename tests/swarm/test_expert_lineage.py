"""Expert lineage records: starting one from a saved mission, the running ones, stopping one and reading its
mission."""

import json
from dataclasses import asdict, replace
from typing import Any

import pytest

from scripts.handoff.resolve import Resolver
from scripts.swarm import expert_lineage
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.swarm.expert_fixture import (
    ADDRESS,
    DIGEST,
    EXPERT,
    MASTER,
    SECOND_EXPERT,
    TITLE,
    indexed,
    started,
    store,
    stored,
)
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit
_fixtures = (store,)


@pytest.mark.feature("expert-mission-and-lineage")
def test_start_saves_the_mission_and_records_a_running_lineage(store: RedisStore) -> None:
    lineage = started(store)
    assert lineage == expert_lineage.Lineage(
        id="expert-1",
        seat="expert-1@sw",
        mission=ADDRESS,
        title=TITLE,
        source="/missions/batch.md",
        effort="xhigh",
        state="running",
        started_by=MASTER,
        started_at=1_000,
    )
    assert (lineage.ended_by, lineage.ended_at, lineage.note) == ("", 0, "")
    assert expert_lineage.get(store, "sw", "expert-1") == lineage
    assert expert_lineage.running(store, "sw") == [lineage]
    assert expert_lineage.mission(store, "sw", lineage) == GOOD
    assert stored(store, "expert-missions") == {DIGEST: GOOD}
    assert {name: json.loads(raw) for name, raw in stored(store, "expert-lineages").items()} == {
        "expert-1": asdict(lineage)
    }
    assert indexed(store) == {"expert-1"}
    assert store.redis.hgetall(store.key("other", "expert-lineages")) == {}
    assert indexed(store, "other") == set()


@pytest.mark.feature("expert-mission-and-lineage")
def test_each_start_takes_the_next_lineage_and_the_same_text_keeps_one_address(store: RedisStore) -> None:
    ids = [started(store, at=5).id for _ in range(11)]
    assert ids == [f"expert-{number}" for number in range(1, 12)]
    assert [row.id for row in expert_lineage.running(store, "sw")] == ids
    assert {row.mission for row in expert_lineage.running(store, "sw")} == {ADDRESS}
    assert stored(store, "expert-missions") == {DIGEST: GOOD}
    assert expert_lineage.start(store, "other", GOOD, "", "max", "operator", 1).id == "expert-1"
    assert [row.id for row in expert_lineage.running(store, "other")] == ["expert-1"]


def test_a_lineage_takes_its_number_from_the_expert_sequence_of_its_swarm(store: RedisStore) -> None:
    store.redis.set(store.key("sw", "expert-seq"), 41)
    assert started(store).id == "expert-42"
    assert store.redis.get(store.key("sw", "expert-seq")) == "42"


def test_the_running_lineages_list_in_the_order_started_whatever_their_ids(store: RedisStore) -> None:
    late, early = started(store, at=9), started(store, at=3)
    assert [row.id for row in expert_lineage.running(store, "sw")] == [early.id, late.id] == ["expert-2", "expert-1"]
    assert expert_lineage.running(store, "other") == []


def test_the_lineages_that_owe_their_closing_steps_list_in_the_order_of_their_ids(store: RedisStore) -> None:
    ids = [started(store, at=5).id for _ in range(10)]
    for name in (ids[9], ids[1]):
        expert_lineage.complete(store, "sw", name, EXPERT, "the mission proof", 9)
    assert [row.id for row in expert_lineage.closing(store, "sw")] == ["expert-2", "expert-10"]
    assert expert_lineage.closing(store, "other") == []


def test_a_malformed_mission_is_refused_with_every_fault_and_takes_no_lineage(store: RedisStore) -> None:
    text = GOOD.replace("# Mission: ", "# ").replace("## Evidence\n", "")
    with pytest.raises(SwarmError) as refused:
        expert_lineage.start(store, "sw", text, "", "xhigh", MASTER, 1)
    assert str(refused.value) == (
        "mission refused; fix the document and start again:\n"
        "- the first line must be # Mission: followed by the mission's title\n"
        "- ## Evidence must appear once and appears 0 times"
    )
    assert stored(store, "expert-missions") == stored(store, "expert-lineages") == {}
    assert indexed(store) == set()
    assert started(store).id == "expert-1"


@pytest.mark.parametrize("effort", ["high", "medium", "", "XHIGH", "maximum"])
def test_an_effort_outside_xhigh_and_max_is_refused_and_takes_no_lineage(store: RedisStore, effort: str) -> None:
    with pytest.raises(SwarmError) as refused:
        started(store, effort=effort)
    assert str(refused.value) == f"an expert runs at effort xhigh or max, not {effort}"
    assert stored(store, "expert-missions") == stored(store, "expert-lineages") == {}
    assert indexed(store) == set()
    assert started(store, effort="max").id == "expert-1"


def test_an_unknown_lineage_is_refused_by_name(store: RedisStore) -> None:
    started(store)
    for call in (
        lambda: expert_lineage.get(store, "sw", "expert-9"),
        lambda: expert_lineage.get(store, "other", "expert-1"),
        lambda: expert_lineage.stop(store, "sw", "expert-9", MASTER, "gone", 2),
    ):
        with pytest.raises(SwarmError) as refused:
            call()
        assert str(refused.value) in (
            "swarm sw has no expert lineage expert-9",
            "swarm other has no expert lineage expert-1",
        )


@pytest.mark.feature("expert-mission-and-lineage")
def test_stop_ends_a_running_lineage_with_its_note_and_only_once(store: RedisStore) -> None:
    lineage, other = started(store, at=1), started(store, at=2)
    ended = expert_lineage.stop(store, "sw", "expert-1", "operator", "the order changed", 2_000)
    assert ended == replace(lineage, state="stopped", ended_by="operator", ended_at=2_000, note="the order changed")
    assert expert_lineage.get(store, "sw", "expert-1") == ended
    assert expert_lineage.running(store, "sw") == [other]
    assert indexed(store) == {"expert-2"}
    with pytest.raises(SwarmError) as refused:
        expert_lineage.stop(store, "sw", "expert-1", MASTER, "again", 3_000)
    assert str(refused.value) == "expert lineage expert-1 is stopped, not running"
    assert expert_lineage.get(store, "sw", "expert-1") == ended
    assert expert_lineage.running(store, "sw") == [other]


@pytest.mark.parametrize("note", ["", "   ", "\n\t"])
def test_a_stop_without_a_note_is_refused_and_leaves_the_lineage_running(store: RedisStore, note: str) -> None:
    lineage = started(store)
    with pytest.raises(SwarmError) as refused:
        expert_lineage.stop(store, "sw", "expert-1", MASTER, note, 2)
    assert str(refused.value) == "stopping a lineage needs a note that says why"
    assert expert_lineage.get(store, "sw", "expert-1") == lineage
    assert expert_lineage.running(store, "sw") == [lineage]


def test_a_lineage_that_ends_while_a_stop_is_written_is_not_ended_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    server = fakeredis.FakeServer()
    store, rival = (RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True)) for _ in range(2))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    started(store)
    real = expert_lineage.replace
    raced: list[str] = []

    def racing(found: Any, **changes: Any) -> Any:
        if not raced:
            raced.append("rival")
            expert_lineage.stop(rival, "sw", "expert-1", "operator", "rival got there first", 7)
        return real(found, **changes)

    monkeypatch.setattr(expert_lineage, "replace", racing)
    with pytest.raises(SwarmError) as refused:
        expert_lineage.stop(store, "sw", "expert-1", MASTER, "mine", 8)
    assert str(refused.value) == "expert lineage expert-1 is stopped, not running"
    kept = expert_lineage.get(store, "sw", "expert-1")
    assert (kept.ended_by, kept.ended_at, kept.note) == ("operator", 7, "rival got there first")
    assert expert_lineage.running(store, "sw") == []


def test_a_saved_mission_that_is_gone_or_changed_is_refused(store: RedisStore) -> None:
    lineage = started(store)
    missing = f"the saved mission of lineage expert-1 is missing or does not match {ADDRESS}"
    store.redis.hset(store.key("sw", "expert-missions"), DIGEST, GOOD + "one more line\n")
    with pytest.raises(SwarmError) as changed:
        expert_lineage.mission(store, "sw", lineage)
    store.redis.hdel(store.key("sw", "expert-missions"), DIGEST)
    with pytest.raises(SwarmError) as gone:
        expert_lineage.mission(store, "sw", lineage)
    assert str(changed.value) == str(gone.value) == missing


@pytest.mark.feature("expert-mission-and-lineage")
def test_a_mission_address_resolves_while_the_swarm_holds_that_text(store: RedisStore) -> None:
    resolves = Resolver("sw", store.redis)
    assert not resolves(ADDRESS)
    assert not expert_lineage.saved(store, "sw", DIGEST)
    started(store)
    assert resolves(ADDRESS)
    assert expert_lineage.saved(store, "sw", DIGEST)
    assert not resolves(f"mission:other/{DIGEST}")
    assert not resolves(f"mission:sw/{'0' * 64}")
    assert not resolves("mission:sw")
    assert not Resolver("sw", None)(ADDRESS)


@pytest.mark.feature("expert-launch-failure-reason")
def test_the_last_failed_launch_is_kept_beside_a_running_lineage_and_beside_no_other(store: RedisStore) -> None:
    lineage = started(store)
    record = stored(store, "expert-lineages")
    assert expert_lineage.launch_failures(store, "sw") == {}
    expert_lineage.launch_failed(store, "sw", "expert-1", EXPERT, "herdr down", 5)
    expert_lineage.launch_failed(store, "sw", "expert-1", SECOND_EXPERT, "no pane", 6)
    kept = {"expert-1": {"at": 6, "agent": SECOND_EXPERT, "reason": "no pane"}}
    assert expert_lineage.launch_failures(store, "sw") == kept
    assert (stored(store, "expert-lineages"), indexed(store)) == (record, {"expert-1"})
    assert expert_lineage.get(store, "sw", "expert-1") == lineage
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "superseded", 7)
    assert expert_lineage.launch_failures(store, "sw") == {}
    expert_lineage.launch_failed(store, "sw", "expert-1", EXPERT, "too late", 8)
    expert_lineage.launch_failed(store, "sw", "expert-9", EXPERT, "no such lineage", 9)
    assert expert_lineage.launch_failures(store, "sw") == {}
    assert json.loads(stored(store, "expert-launch-failures")["expert-1"])["reason"] == "no pane"
    assert set(stored(store, "expert-lineages")) == {"expert-1"}


@pytest.mark.feature("expert-launch-failure-reason")
def test_a_launch_that_succeeds_drops_the_failure_kept_before_it(store: RedisStore) -> None:
    started(store)
    second = started(store, at=2)
    expert_lineage.launch_recovered(store, "sw", "expert-1")
    expert_lineage.launch_failed(store, "sw", "expert-1", EXPERT, "herdr down", 5)
    expert_lineage.launch_failed(store, "sw", second.id, SECOND_EXPERT, "no pane", 6)
    expert_lineage.launch_recovered(store, "sw", "expert-1")
    assert expert_lineage.launch_failures(store, "sw") == {
        second.id: {"at": 6, "agent": SECOND_EXPERT, "reason": "no pane"}
    }
