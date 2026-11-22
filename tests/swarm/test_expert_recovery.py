"""An expert that is lost while its lineage runs is resumed in its own conversation before a fresh one takes its
seat, its mail goes on with the seat, and the mail of a lineage that ended goes back to the master."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hooks.context import context_recycle
from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import dead_resume, expert_lineage, expert_seat, resume
from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord, RedisStore
from tests.handoff.test_mission_proof import VALID as PROOF
from tests.swarm.expert_fixture import ENGINEER, MASTER, acting, run, started, store
from tests.swarm.test_dead_resume import ResumingRuntime
from tests.swarm.test_tick import tasks

pytestmark = pytest.mark.unit
_fixtures = (store,)

NOW = 1_800_000_000_000
MINUTE = 60_000
RETURNED = "expert lineage expert-1 is {state} and no expert holds its seat any more"


@pytest.fixture
def ledger() -> Any:
    return tasks(("t1", "eng"))


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)
    return ResumingRuntime()


def seated(found: RedisStore, runtime: Any) -> AgentRecord:
    """Launch the expert of the started lineage and give it the conversation a resume needs."""
    expert_seat.run("sw", found.config("sw"), found, runtime, NOW)
    [expert] = [agent for agent in found.agents("sw") if agent.lane == "expert"]
    known = replace(expert, conversation_id="conversation", profile="expert")
    found.put_agent("sw", known)
    runtime.live.discard(known.name)
    return known


@pytest.mark.feature("expert-seat-recovery")
def test_a_lost_expert_of_a_running_lineage_is_resumed_in_its_own_conversation_and_seat(
    store: RedisStore, ledger: Any, runtime: Any
) -> None:
    started(store)
    expert = seated(store, runtime)
    assert dead_resume.resumable(store, "sw", expert, ledger) is True
    assert dead_resume.reopen("sw", store, ledger, runtime, expert, NOW + MINUTE) is True
    assert runtime.resumed == [expert]
    [reopened] = [agent for agent in store.agents("sw") if agent.lane == "expert"]
    assert (reopened.name, reopened.seat, reopened.conversation_id) == (expert.name, expert.seat, "conversation")
    assert ledger.notes[-1] == f"Resumed {expert.name} in its own conversation and seat after its pane ended."


@pytest.mark.feature("expert-seat-recovery")
def test_an_expert_whose_lineage_ended_is_not_resumed(store: RedisStore, ledger: Any, runtime: Any) -> None:
    started(store)
    expert = seated(store, runtime)
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "the order changed", NOW + 1)
    assert dead_resume.resumable(store, "sw", expert, ledger) is False
    assert dead_resume.reopen("sw", store, ledger, runtime, expert, NOW + MINUTE) is False
    assert runtime.resumed == []


def test_an_expert_whose_lineage_record_is_gone_is_not_resumed_and_its_mail_is_closed(
    store: RedisStore, ledger: Any, runtime: Any
) -> None:
    started(store)
    expert = seated(store, runtime)
    inbox = InboxStore(store.redis)
    asked = inbox.send(MASTER, expert.name, "Which batch comes first")
    store.redis.hdel(store.key("sw", "expert-lineages"), "expert-1")
    store.redis.srem(store.key("sw", "expert-running"), "expert-1")
    assert expert_lineage.runs(store, "sw", "expert-1") is False
    assert dead_resume.resumable(store, "sw", expert, ledger) is False
    assert dead_resume.reopen("sw", store, ledger, runtime, expert, NOW + MINUTE) is False
    assert runtime.resumed == []
    assert tick_module._drop("sw", store, ledger, {}, expert) == ""
    assert inbox.get(asked.id).state == "cancelled"


@pytest.mark.parametrize(
    "author,reason",
    [
        ("expert@a1b2c3-0009", "the handoff on its seat was never confirmed"),
        ("", "a handoff was submitted"),
    ],
)
def test_an_expert_with_a_handoff_on_its_seat_is_not_resumed_and_the_notice_names_whose_it_is(
    store: RedisStore, ledger: Any, runtime: Any, author: str, reason: str
) -> None:
    started(store)
    expert = seated(store, runtime)
    envelope = {"agent": author or expert.name, "reason": "recycle"}
    store.put_handoff("sw", "expert-1", "the handoff on the seat", seat=expert.seat, envelope=envelope)
    assert dead_resume.resumable(store, "sw", expert, ledger) is False
    assert dead_resume.reopen("sw", store, ledger, runtime, expert, NOW + MINUTE) is False
    assert ledger.notes[-1] == f"Starting a fresh agent after {expert.name} ended because {reason}."


def test_the_seat_mail_a_dropped_expert_had_taken_goes_back_to_its_seat_while_its_lineage_runs(
    store: RedisStore, ledger: Any, runtime: Any
) -> None:
    started(store)
    expert = seated(store, runtime)
    inbox = InboxStore(store.redis)
    taken = inbox.send(MASTER, expert.seat, "Read the new evidence")
    inbox.deliver(taken.id, expert.name)
    assert inbox.get(taken.id).state != "pending"
    assert tick_module._drop("sw", store, ledger, {}, expert) == ""
    back = inbox.get(taken.id)
    assert (back.state, back.address) == ("pending", expert.seat)


def test_mail_that_already_left_the_seat_of_an_ended_lineage_is_not_moved_again(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    lineage = started(store)
    inbox = InboxStore(store.redis)
    waiting = inbox.send(ENGINEER, lineage.seat, "Read the new evidence")
    listed = inbox.get(waiting.id)
    expert_seat.returned(store, "sw", lineage.seat, "expert-1", "stopped")
    moved = inbox.get(waiting.id)
    assert (moved.state, moved.address) == ("pending", "master@sw")
    monkeypatch.setattr(InboxStore, "open_items", lambda self, address: [listed])
    expert_seat.returned(store, "sw", lineage.seat, "expert-1", "complete")
    assert inbox.get(waiting.id) == moved


@pytest.mark.feature("expert-seat-recovery")
def test_an_expert_that_keeps_ending_gets_no_task_folder_handoff_and_its_successor_a_recovery_handoff(
    store: RedisStore, ledger: Any, runtime: Any
) -> None:
    started(store)
    expert = seated(store, runtime)
    for resumed in range(dead_resume.RESUME_ATTEMPTS):
        dead_resume.note_resume(store, "sw", expert, NOW + resumed)
    assert dead_resume.reopen("sw", store, ledger, runtime, expert, NOW + MINUTE) is False
    assert ledger.notes[-1] == (
        f"Starting a fresh agent after {expert.name} ended because it ended again after each of its last 3 resumes."
    )
    assert store.handoff("sw", "expert-1") == "" and transfers.list_transfers(store, "sw") == []
    assert tick_module._drop("sw", store, ledger, {}, expert) == ""
    written, spawned = expert_seat.run("sw", store.config("sw"), store, runtime, NOW + 2 * MINUTE)
    assert written == f"wrote the recovery handoff of lineage expert-1: {expert.name} was lost and left none"
    assert spawned.endswith("on lineage expert-1 as its expert number 2")
    assert store.handoff("sw", "expert-1").startswith("# Expert handoff\n")


def test_a_worker_that_keeps_ending_still_gets_its_task_folder_handoff(store: RedisStore, ledger: Any) -> None:
    worker = AgentRecord(ENGINEER, "eng", "t1", seat="eng-1@sw")
    dead_resume._hand_off_capped(store, "sw", worker, NOW)
    assert store.handoff("sw", "t1").startswith("## Intent\nContinue task t1; ")
    [row] = transfers.list_transfers(store, "sw")
    assert (row["task"], row["predecessor"]) == ("t1", ENGINEER)


@pytest.mark.feature("expert-seat-recovery")
def test_the_mail_of_an_expert_dropped_while_its_lineage_runs_goes_on_with_its_seat(
    store: RedisStore, ledger: Any, runtime: Any
) -> None:
    started(store)
    expert = seated(store, runtime)
    inbox = InboxStore(store.redis)
    asked = inbox.send(MASTER, expert.name, "Which batch comes first")
    waiting = inbox.send(MASTER, expert.seat, "Read the new evidence")
    assert tick_module._drop("sw", store, ledger, {}, expert) == ""
    assert [(inbox.get(item.id).state, inbox.get(item.id).address) for item in (asked, waiting)] == [
        ("pending", expert.seat),
        ("pending", expert.seat),
    ]


def test_the_mail_of_an_expert_dropped_after_its_lineage_was_stopped_is_closed(
    store: RedisStore, ledger: Any, runtime: Any
) -> None:
    started(store)
    expert = seated(store, runtime)
    inbox = InboxStore(store.redis)
    asked = inbox.send(MASTER, expert.name, "Which batch comes first")
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "the order changed", NOW + 1)
    assert tick_module._drop("sw", store, ledger, {}, expert) == ""
    assert inbox.get(asked.id).state == "cancelled"


@pytest.mark.feature("expert-seat-recovery")
def test_stopping_a_lineage_sends_the_mail_waiting_on_its_seat_back_to_the_master(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lineage = started(store)
    inbox = InboxStore(store.redis)
    waiting = inbox.send(ENGINEER, lineage.seat, "Read the new evidence")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "stop", "expert-1", "--note", "the order changed") == 0
    capsys.readouterr()
    moved = inbox.get(waiting.id)
    assert (moved.state, moved.address, moved.reason) == ("pending", "master@sw", RETURNED.format(state="stopped"))


@pytest.mark.feature("expert-seat-recovery")
def test_a_completed_lineage_sends_the_mail_waiting_on_its_seat_back_to_the_master(
    store: RedisStore, runtime: Any
) -> None:
    lineage = started(store)
    expert = seated(store, runtime)
    inbox = InboxStore(store.redis)
    waiting = inbox.send(ENGINEER, lineage.seat, "Read the new evidence")
    ended = expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    expert_seat.close(store, "sw", ended, PROOF)
    moved = inbox.get(waiting.id)
    assert (moved.state, moved.address, moved.reason) == ("pending", "master@sw", RETURNED.format(state="complete"))


def test_the_seat_pass_sends_mail_that_reached_a_stopped_seat_back_when_it_ends_the_expert(
    store: RedisStore, runtime: Any
) -> None:
    lineage = started(store)
    expert = seated(store, runtime)
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "the order changed", NOW + 1)
    inbox = InboxStore(store.redis)
    late = inbox.send(ENGINEER, lineage.seat, "One more thing")
    assert expert_seat.run("sw", store.config("sw"), store, runtime, NOW + MINUTE) == [
        f"ended expert {expert.name}: lineage expert-1 is stopped"
    ]
    assert (inbox.get(late.id).state, inbox.get(late.id).address) == ("pending", "master@sw")


def test_the_seat_mail_of_an_expert_whose_lineage_record_is_gone_goes_back_to_the_master(
    store: RedisStore, runtime: Any
) -> None:
    lineage = started(store)
    expert = seated(store, runtime)
    store.redis.hdel(store.key("sw", "expert-lineages"), "expert-1")
    store.redis.srem(store.key("sw", "expert-running"), "expert-1")
    inbox = InboxStore(store.redis)
    late = inbox.send(ENGINEER, lineage.seat, "One more thing")
    assert expert_seat.run("sw", store.config("sw"), store, runtime, NOW + MINUTE) == [
        f"ended expert {expert.name}: lineage expert-1 is unknown"
    ]
    moved = inbox.get(late.id)
    assert (moved.state, moved.address, moved.reason) == ("pending", "master@sw", RETURNED.format(state="unknown"))


@pytest.mark.feature("expert-seat-recovery")
def test_a_restored_expert_is_told_to_read_its_mission_and_its_seat_recap(store: RedisStore) -> None:
    expert = AgentRecord("expert@a1b2c3-0001", "expert", "expert-1", seat="expert-1@sw")
    text = resume.restored_text("sw", expert, "agentihooks ledger read sw")
    assert text == (
        "The swarm sw was restored from a snapshot and reopened this conversation of yours as expert@a1b2c3-0001, on "
        "task expert-1. Time passed and other agents may have moved the work. Before acting, re-read your mission, "
        "printed by agentihooks swarm sw expert mission, and the recap of your seat, then continue from what they say."
    )


def test_a_restored_worker_and_a_restored_master_are_told_what_they_were_told_before(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = AgentRecord(MASTER, "master", "master", seat="master@sw")
    told = resume.restored_text("sw", master, "the ledger command")
    assert "re-read the ledger, printed by the ledger command, then" in told
    worker = resume.restored_text("sw", AgentRecord(ENGINEER, "eng", "t1"), "the ledger command")
    assert "re-read your task folder " in worker and "(steering.md, progress.md, proof.md) and the ledger" in worker
