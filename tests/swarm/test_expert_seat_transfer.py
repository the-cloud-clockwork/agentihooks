from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from scripts.handoff import transfers
from scripts.swarm import expert_seat
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm.tick import SpawnError
from tests.handoff.test_expert_contract import VALID as HANDOFF
from tests.swarm.expert_fixture import started, store
from tests.swarm.test_expert_seat import MINUTE, NEXT, NOW, ROOM, experts, go, handed_off, launched, rows
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.unit
_fixtures = (store,)


def pending(found: RedisStore, agent: AgentRecord, identity: str, at: int, text: str = HANDOFF) -> dict[str, Any]:
    with patch.object(transfers, "uuid4", return_value=SimpleNamespace(hex=identity)):
        return transfers.record(found, "sw", agent, "recycle", text, at)


@pytest.mark.feature("expert-newest-seat-transfer")
def test_first_expert_needs_no_transfer(store: RedisStore) -> None:
    lineage, runtime = started(store), FakeRuntime()
    first = launched(store, runtime)
    assert runtime.tasks[-1]["transfer"] is None
    assert runtime.tasks[-1]["handoff"] == ""
    assert runtime.tasks[-1]["seat"] == first.seat == lineage.seat
    assert rows(store) == []


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("tied", [False, True])
def test_newest_pending_transfer_uses_timestamp_then_identifier(store: RedisStore, tied: bool) -> None:
    started(store)
    runtime = FakeRuntime()
    first = launched(store, runtime)
    with patch.object(transfers, "uuid4", return_value=SimpleNamespace(hex="z-current")):
        current = handed_off(store, first)
    obsolete = pending(store, first, "a-obsolete", NOW if tied else NOW - 1, "obsolete handoff")
    successor = launched(store, runtime, NOW + MINUTE)
    task = runtime.tasks[-1]
    assert (task["transfer"]["id"], task["transfer"]["next"], task["handoff"]) == (current["id"], NEXT, HANDOFF)
    assert task["transfer"]["seat"] == task["seat"] == successor.seat
    assert transfers.get(store, "sw", obsolete["id"]) == obsolete
    assert transfers.confirm(store, "sw", current["id"], successor, NEXT, NOW + MINUTE)["continuity"] == {
        "state": "confirmed",
        "at": NOW + MINUTE,
        "by": successor.name,
        "next": NEXT,
    }


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("tied", [False, True])
def test_selected_transfer_supplies_the_runtime_handoff(store: RedisStore, tied: bool) -> None:
    started(store)
    runtime = FakeRuntime()
    first = launched(store, runtime)
    current = pending(store, first, "z-current", NOW)
    stored_text = HANDOFF.replace("Finish slice two", "Inspect the older document")
    obsolete = handed_off(store, first, stored_text, NOW if tied else NOW - 1)
    assert obsolete["next"] != NEXT
    successor = launched(store, runtime, NOW + MINUTE)
    task = runtime.tasks[-1]
    assert task["handoff"] == task["transfer"]["handoff"] == HANDOFF
    assert (task["generation"], task["transfer"]["id"], task["transfer"]["next"]) == (2, current["id"], NEXT)
    assert task["transfer"]["seat"] == task["seat"] == successor.seat
    assert task["transfer"]["generation"] == store.seats.occupant(successor.seat).generation
    assert transfers.get(store, "sw", obsolete["id"]) == obsolete
    assert transfers.confirm(store, "sw", current["id"], successor, NEXT, NOW + MINUTE)["continuity"] == {
        "state": "confirmed",
        "at": NOW + MINUTE,
        "by": successor.name,
        "next": NEXT,
    }


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize(("clock_offset", "retry_offset"), [(-1, 1), (0, 1), (2, 2)])
def test_failed_retry_stays_newest_when_the_clock_does_not_advance(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, clock_offset: int, retry_offset: int
) -> None:
    started(store)
    runtime = FakeRuntime()
    first = launched(store, runtime)
    with patch.object(transfers, "uuid4", return_value=SimpleNamespace(hex="z-current")):
        current = handed_off(store, first)
    monkeypatch.setattr(transfers, "now_ms", lambda: NOW + clock_offset)
    with patch.object(transfers, "uuid4", return_value=SimpleNamespace(hex="a-retry")):
        assert go(store, FakeRuntime(fail=True), NOW + MINUTE) == [
            "expert spawn failed on lineage expert-1: herdr down"
        ]
    failed = transfers.get(store, "sw", current["id"])
    retry = transfers.get(store, "sw", "a-retry")
    assert failed["binding"]["at"] == NOW + clock_offset
    assert retry["retry_of"] == current["id"]
    successor = launched(store, runtime, NOW + 2 * MINUTE)
    task = runtime.tasks[-1]
    assert (task["transfer"]["id"], task["transfer"]["next"], task["handoff"]) == (retry["id"], NEXT, HANDOFF)
    assert task["transfer"]["successor"] == successor.name
    assert retry["at"] == NOW + retry_offset
    assert transfers.get(store, "sw", current["id"]) == failed


@pytest.mark.feature("expert-newest-seat-transfer")
def test_foreign_transfer_cannot_replace_the_current_seat_handoff(store: RedisStore) -> None:
    lineage, runtime = started(store), FakeRuntime()
    first = launched(store, runtime)
    current = handed_off(store, first)
    foreign = pending(store, replace(first, seat="expert-foreign@sw"), "foreign", NOW + 1, "foreign handoff")
    successor = launched(store, runtime, NOW + MINUTE)
    task = runtime.tasks[-1]
    assert (task["transfer"]["id"], task["transfer"]["handoff"], task["transfer"]["next"]) == (
        current["id"],
        HANDOFF,
        NEXT,
    )
    assert task["handoff"] == HANDOFF
    assert task["transfer"]["seat"] == task["seat"] == successor.seat == lineage.seat
    assert transfers.get(store, "sw", foreign["id"]) == foreign


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("tied", [False, True])
def test_lost_successor_ignores_obsolete_pending_history(
    store: RedisStore, confirmed: bool, foreign: bool, tied: bool
) -> None:
    lineage, runtime = started(store), FakeRuntime()
    first = launched(store, runtime)
    author = replace(first, seat="expert-foreign@sw") if foreign else first
    obsolete = pending(store, author, "a-obsolete", NOW if tied else NOW - 1, "obsolete handoff")
    with patch.object(transfers, "uuid4", return_value=SimpleNamespace(hex="z-current")):
        current = handed_off(store, first)
    second = launched(store, runtime, NOW + MINUTE)
    if confirmed:
        transfers.confirm(store, "sw", current["id"], second, NEXT, NOW + MINUTE)
        expert_seat.drop_confirmed(store, "sw", second)
    previous = transfers.get(store, "sw", current["id"])
    generation = store.seats.occupant(lineage.seat).generation
    store.drop_agent("sw", second.name)
    successor = launched(store, runtime, NOW + 2 * MINUTE)
    task = runtime.tasks[-1]
    selected = task["transfer"]
    assert selected["id"] not in {obsolete["id"], current["id"]}
    assert selected == transfers.get(store, "sw", selected["id"])
    assert selected["handoff"] == task["handoff"] == store.handoff("sw", lineage.id)
    assert selected["seat"] == task["seat"] == successor.seat == lineage.seat
    assert selected["successor"] == successor.name
    assert selected["predecessor"] == second.name
    assert selected["generation"] == store.seats.occupant(lineage.seat).generation == generation + 1
    assert task["generation"] == 3
    assert transfers.get(store, "sw", obsolete["id"]) == obsolete
    assert transfers.get(store, "sw", current["id"]) == previous
    assert len(rows(store)) == 3
    if confirmed:
        assert task["handoff"] != HANDOFF
        assert task["handoff_envelope"]["reason"] == expert_seat.REASON
    else:
        assert (selected["handoff"], selected["next"]) == (HANDOFF, NEXT)
        assert task["handoff_envelope"] == {"reason": "recycle"}
    with pytest.raises(SwarmError) as wrong:
        transfers.confirm(store, "sw", selected["id"], successor, "Wrong next action", NOW + 3 * MINUTE)
    assert str(wrong.value) == "Confirm the first Next action exactly as written in the handoff"
    with pytest.raises(SwarmError) as stale:
        transfers.confirm(store, "sw", selected["id"], second, selected["next"], NOW + 3 * MINUTE)
    assert str(stale.value) == "Only the current successor occupant can confirm this handoff"
    accepted = transfers.confirm(store, "sw", selected["id"], successor, selected["next"], NOW + 3 * MINUTE)
    assert accepted["continuity"] == {
        "state": "confirmed",
        "at": NOW + 3 * MINUTE,
        "by": successor.name,
        "next": selected["next"],
    }


@pytest.mark.feature("expert-newest-seat-transfer")
def test_foreign_history_alone_does_not_suppress_recovery(store: RedisStore) -> None:
    lineage, runtime = started(store), FakeRuntime()
    first = launched(store, runtime)
    foreign = pending(store, replace(first, seat="expert-foreign@sw"), "foreign", NOW, "foreign handoff")
    store.drop_agent("sw", first.name)
    successor = launched(store, runtime, NOW + MINUTE)
    task = runtime.tasks[-1]
    assert task["transfer"]["id"] != foreign["id"]
    assert task["handoff"] == task["transfer"]["handoff"] != ""
    assert task["handoff_envelope"]["reason"] == expert_seat.REASON
    assert task["transfer"]["seat"] == successor.seat == lineage.seat
    assert transfers.get(store, "sw", foreign["id"]) == foreign


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("room", [False, True])
def test_retry_keeps_the_current_transfer_and_obsolete_history(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, room: bool
) -> None:
    started(store)
    runtime = FakeRuntime()
    first = launched(store, runtime)
    obsolete = pending(store, first, "obsolete", NOW - 1, "obsolete handoff")
    current = handed_off(store, first)
    monkeypatch.setattr(transfers, "now_ms", lambda: NOW + 2 * MINUTE)
    refused = FakeRuntime(crash=SpawnError(ROOM, "unavailable")) if room else FakeRuntime(fail=True)
    actions = go(store, refused, NOW + MINUTE)
    assert experts(store) == []
    assert transfers.get(store, "sw", obsolete["id"]) == obsolete
    kept = transfers.get(store, "sw", current["id"])
    if room:
        assert actions == [f"lineage expert-1 waits: {ROOM}"]
        assert (kept["successor"], kept["binding"]) == ("", {"state": "pending"})
        assert len(rows(store)) == 2
        retry = kept
    else:
        assert actions == ["expert spawn failed on lineage expert-1: herdr down"]
        assert (kept["binding"]["state"], kept["binding"]["reason"]) == ("absent", "herdr down")
        [retry] = [row for row in rows(store) if row["id"] not in {obsolete["id"], current["id"]}]
        assert (retry["retry_of"], retry["successor"], retry["binding"]) == (current["id"], "", {"state": "pending"})
    successor = launched(store, runtime, NOW + 3 * MINUTE)
    task = runtime.tasks[-1]
    assert (task["transfer"]["id"], task["transfer"]["next"], task["handoff"]) == (retry["id"], NEXT, HANDOFF)
    assert task["transfer"]["successor"] == successor.name
    assert transfers.get(store, "sw", obsolete["id"]) == obsolete
    if not room:
        assert transfers.get(store, "sw", current["id"]) == kept


@pytest.mark.feature("expert-newest-seat-transfer")
def test_generic_attachment_preserves_existing_pending_selection(store: RedisStore) -> None:
    started(store)
    first = launched(store, FakeRuntime())
    obsolete = pending(store, first, "obsolete", NOW - 1)
    current = pending(store, first, "current", NOW)
    attached = transfers.attach(store, "sw", first)
    assert attached is not None and attached["id"] == current["id"]
    worker = AgentRecord("ordinary-worker", "eng", first.task, seat="eng-99@sw")
    selected = transfers.attach(store, "sw", worker)
    assert selected is not None and selected["id"] == obsolete["id"]
    assert selected["seat"] == worker.seat
    assert transfers.get(store, "sw", current["id"]) == attached


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("tied", [False, True])
def test_scoped_attachment_never_revives_superseded_pending_history(
    store: RedisStore, confirmed: bool, tied: bool
) -> None:
    started(store)
    first = launched(store, FakeRuntime())
    pending(store, first, "a-obsolete", NOW if tied else NOW - 1)
    current = pending(store, first, "z-current", NOW)
    selected = transfers.attach(store, "sw", first, seat=first.seat)
    assert selected is not None and selected["id"] == current["id"]
    if confirmed:
        transfers.confirm(store, "sw", current["id"], first, NEXT, NOW + MINUTE)
    before = rows(store)
    successor = replace(first, name="waiting-expert")
    assert transfers.attach(store, "sw", successor, seat=first.seat) is None
    assert rows(store) == before


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("different", ["seat", "task"])
def test_scoped_attachment_has_no_foreign_fallback(store: RedisStore, different: str) -> None:
    started(store)
    first = launched(store, FakeRuntime())
    foreign = replace(first, **{different: "foreign"})
    original = pending(store, foreign, "foreign", NOW)
    assert transfers.attach(store, "sw", first, seat=first.seat) is None
    assert rows(store) == [original]


@pytest.mark.feature("expert-newest-seat-transfer")
@pytest.mark.parametrize("different", ["seat", "task"])
def test_scoped_attachment_keeps_newer_foreign_history(store: RedisStore, different: str) -> None:
    started(store)
    first = launched(store, FakeRuntime())
    current = pending(store, first, "current", NOW)
    foreign = pending(store, replace(first, **{different: "foreign"}), "foreign", NOW + 1)
    selected = transfers.attach(store, "sw", first, seat=first.seat)
    assert selected is not None and selected["id"] == current["id"]
    assert selected["successor"] == first.name
    assert selected["seat"] == first.seat
    assert transfers.get(store, "sw", foreign["id"]) == foreign
