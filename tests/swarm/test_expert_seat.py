"""The expert seats pass: one expert per running lineage, the next one after an accepted handoff, a handoff left for
an expert that was lost, the end of an ended lineage's expert, and a lineage started from another session's handoff."""

from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.handoff import expert as handoff_expert
from scripts.handoff import transfers
from scripts.handoff.resolve import Resolver
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import cli, expert_lineage, expert_seat, naming, prompt, timing
from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_workspace
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Result
from tests.handoff.test_expert_contract import VALID as HANDOFF
from tests.handoff.test_mission_proof import VALID as PROOF
from tests.swarm.expert_fixture import ENGINEER, MASTER, TITLE, acting, indexed, mission_file, run, started, store
from tests.swarm.test_expert_complete import RECAP
from tests.swarm.test_expert_handoff_command import V2
from tests.swarm.test_expert_mission import GOOD
from tests.swarm.test_tick import FakeRuntime
from tests.tick.test_engine import FakeStep, View

pytestmark = pytest.mark.unit
_fixtures = (store, mission_file)

NOW = 1_800_000_000_000
MINUTE = 60_000
NEXT = "1. Finish slice two; done when its test passes."
SEED_NEXT = "1. Make seam two green; done when its test passes."
SESSION = "s-261122-002433"
ROOM = "no hive has room, and the local machine runs 4 of its 4 agents"
REFUSED = "herdr refused the pane: exit 3"


def go(found: RedisStore, runtime: Any, now: int = NOW) -> list[str]:
    return expert_seat.run("sw", found.config("sw"), found, runtime, now)


def experts(found: RedisStore) -> list[AgentRecord]:
    return [agent for agent in found.agents("sw") if agent.lane == "expert"]


def launched(found: RedisStore, runtime: Any, now: int = NOW) -> AgentRecord:
    """Run one pass and return the expert it seated."""
    before = {agent.name for agent in experts(found)}
    go(found, runtime, now)
    [new] = [agent for agent in experts(found) if agent.name not in before]
    return new


def handed_off(found: RedisStore, expert: AgentRecord, text: str = HANDOFF, at: int = NOW) -> dict[str, Any]:
    """Leave the seat as the handoff command does: the document stored, its transfer recorded, the expert gone."""
    found.put_handoff("sw", expert.task, text, seat=expert.seat, envelope={"reason": "recycle"})
    row: dict[str, Any] = transfers.record(found, "sw", expert, "recycle", text, at)
    found.drop_agent("sw", expert.name)
    return row


def rows(found: RedisStore) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = transfers.list_transfers(found, "sw")
    return listed


class Placing(FakeRuntime):
    """A fake runtime that keeps the swarm config each spawn was placed with."""

    def __init__(self) -> None:
        super().__init__()
        self.placed_with: list[Any] = []

    def spawn(self, config: Any, lane: str, name: str, task: dict[str, Any]) -> Any:
        self.placed_with.append(config)
        return super().spawn(config, lane, name, task)


@pytest.mark.feature("expert-seat-step")
@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_a_started_lineage_gets_one_expert_on_its_seat_with_the_launch_an_expert_is_held_to(
    store: RedisStore, effort: str
) -> None:
    lineage, runtime = started(store, effort), Placing()
    actions = go(store, runtime)
    [expert] = experts(store)
    assert actions == [f"spawned expert {expert.name} on lineage expert-1 as its expert number 1"]
    assert runtime.placed_with == [store.config("sw")]
    assert (expert.lane, expert.task, expert.seat, expert.state) == ("expert", "expert-1", lineage.seat, "working")
    parsed = naming.parse(expert.name)
    assert parsed is not None and parsed.kind == "expert"
    assert runtime.spawned == [("expert", expert.name, "expert-1")]
    task = runtime.tasks[0]
    assert {key: task[key] for key in ("id", "title", "mission", "generation", "handoff", "transfer")} == {
        "id": "expert-1",
        "title": TITLE,
        "mission": lineage.mission,
        "generation": 1,
        "handoff": "",
        "transfer": None,
    }
    assert (task["requires_harness"], task["expert_effort"], task["seat"]) == ("claude", effort, lineage.seat)
    assert expert_lineage.launches(store, "sw", "expert-1") == 1
    assert store.seats.occupant(lineage.seat).occupant == expert.name


@pytest.mark.feature("expert-seat-step")
def test_a_seat_that_holds_a_record_in_any_state_gets_no_second_expert(store: RedisStore) -> None:
    started(store)
    runtime = FakeRuntime()
    expert = launched(store, runtime)
    assert go(store, runtime, NOW + MINUTE) == []
    store.put_agent("sw", replace(expert, state="finished"))
    assert go(store, runtime, NOW + 2 * MINUTE) == []
    assert len(runtime.spawned) == 1 and expert_lineage.launches(store, "sw", "expert-1") == 1


def test_each_running_lineage_gets_its_own_expert_in_the_order_started(store: RedisStore) -> None:
    started(store, at=1_000)
    started(store, at=2_000)
    runtime = FakeRuntime()
    assert len(go(store, runtime)) == 2
    assert [task for _, _, task in runtime.spawned] == ["expert-1", "expert-2"]
    assert sorted(agent.task for agent in experts(store)) == ["expert-1", "expert-2"]


@pytest.mark.feature("expert-seat-step")
def test_the_next_expert_follows_an_accepted_handoff_onto_the_same_seat(store: RedisStore) -> None:
    lineage, runtime = started(store), FakeRuntime()
    first = launched(store, runtime)
    row = handed_off(store, first)
    actions = go(store, runtime, NOW + MINUTE)
    [second] = experts(store)
    assert actions == [f"spawned expert {second.name} on lineage expert-1 as its expert number 2"]
    assert second.name != first.name and second.seat == lineage.seat
    task = runtime.tasks[-1]
    assert (task["generation"], task["handoff"]) == (2, HANDOFF)
    assert (task["transfer"]["id"], task["transfer"]["successor"]) == (row["id"], second.name)
    assert task["handoff_envelope"] == {"reason": "recycle"}
    opening = prompt.build("sw", "/repo", "expert", second.name, task)
    assert "You are expert number 2 of lineage expert-1" in opening and row["id"] in opening
    confirmed = transfers.confirm(store, "sw", row["id"], second, NEXT, NOW + 2 * MINUTE)
    assert confirmed["continuity"]["state"] == "confirmed"


@pytest.mark.feature("expert-seat-step")
def test_a_launch_that_fails_leaves_no_record_and_a_failed_binding_and_the_next_pass_tries_again(
    store: RedisStore,
) -> None:
    started(store)
    runtime = FakeRuntime()
    row = handed_off(store, launched(store, runtime))
    assert go(store, FakeRuntime(fail=True), NOW + MINUTE) == ["expert spawn failed on lineage expert-1: herdr down"]
    assert experts(store) == [] and expert_lineage.launches(store, "sw", "expert-1") == 1
    failed, retry = sorted(rows(store), key=lambda found: found["id"] != row["id"])
    assert (failed["binding"]["state"], failed["binding"]["reason"]) == ("absent", "herdr down")
    assert (retry["retry_of"], retry["successor"], retry["binding"]) == (row["id"], "", {"state": "pending"})
    second = launched(store, runtime, NOW + 2 * MINUTE)
    task = runtime.tasks[-1]
    assert (task["generation"], task["handoff"], task["transfer"]["id"]) == (2, HANDOFF, retry["id"])
    assert task["transfer"]["successor"] == second.name


def test_a_first_launch_that_fails_is_tried_again_as_the_first_expert_with_no_handoff(store: RedisStore) -> None:
    started(store)
    assert go(store, FakeRuntime(fail=True)) == ["expert spawn failed on lineage expert-1: herdr down"]
    assert experts(store) == [] and expert_lineage.launches(store, "sw", "expert-1") == 0
    runtime = FakeRuntime()
    expert = launched(store, runtime, NOW + MINUTE)
    assert (runtime.tasks[0]["generation"], runtime.tasks[0]["handoff"], rows(store)) == (1, "", [])
    assert expert.state == "working"


@pytest.mark.parametrize(
    "refused",
    [
        "no hive for expert has room: remote cpu: five minute load 1.17 on 8 CPUs, fits 0 more",
        ROOM,
        ", and the local machine runs 4 of its 4 agents: cpu: five minute load 1.17 on 8 CPUs",
    ],
)
def test_a_launch_refused_for_room_waits_and_hands_its_transfer_back(store: RedisStore, refused: str) -> None:
    started(store)
    row = handed_off(store, launched(store, FakeRuntime()))
    full = FakeRuntime(crash=tick_module.SpawnError(refused, "unavailable"))
    assert go(store, full, NOW + MINUTE) == [f"lineage expert-1 waits: {refused}"]
    assert experts(store) == []
    [kept] = rows(store)
    assert (kept["id"], kept["successor"], kept["binding"]) == (row["id"], "", {"state": "pending"})


def failing(text: str = REFUSED) -> FakeRuntime:
    """A runtime whose launch fails with that error text and no room refusal in it."""
    return FakeRuntime(crash=tick_module.SpawnError(text, "unavailable"))


def failure(found: RedisStore) -> dict[str, Any]:
    kept: dict[str, Any] = expert_lineage.get(found, "sw", "expert-1").launch_failure
    return kept


@pytest.mark.feature("expert-launch-failure-reason")
def test_a_successor_launch_that_fails_keeps_its_error_on_the_binding_and_on_the_lineage(store: RedisStore) -> None:
    started(store)
    row = handed_off(store, launched(store, FakeRuntime()))
    assert go(store, failing(), NOW + MINUTE) == [f"expert spawn failed on lineage expert-1: {REFUSED}"]
    [failed] = [found for found in rows(store) if found["id"] == row["id"]]
    assert (failed["binding"]["state"], failed["binding"]["reason"]) == ("absent", REFUSED)
    assert failed["binding"]["session"] == failed["successor"] != ""
    assert failure(store) == {"at": NOW + MINUTE, "agent": failed["successor"], "reason": REFUSED}


@pytest.mark.feature("expert-launch-failure-reason")
def test_a_first_launch_that_fails_keeps_its_error_on_the_lineage_with_no_transfer_to_carry_it(
    store: RedisStore,
) -> None:
    started(store)
    go(store, failing())
    kept = failure(store)
    assert rows(store) == [] and experts(store) == []
    assert (kept["at"], kept["reason"], naming.lane_of(kept["agent"])) == (NOW, REFUSED, "expert")
    launched(store, FakeRuntime(), NOW + MINUTE)
    assert failure(store) == kept


@pytest.mark.feature("expert-launch-failure-reason")
def test_a_launch_refused_for_room_records_no_failed_launch(store: RedisStore) -> None:
    started(store)
    full = FakeRuntime(crash=tick_module.SpawnError(ROOM, "unavailable"))
    assert go(store, full) == [f"lineage expert-1 waits: {ROOM}"]
    assert failure(store) == {}


@pytest.mark.feature("expert-launch-failure-reason")
def test_the_kept_reason_is_redacted_put_on_one_line_and_cut_at_three_hundred_characters(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(store)
    seen: list[tuple[str, str]] = []

    def redact(text: str, *, mode: str) -> str:
        seen.append((text, mode))
        return text.replace("private", "[gone]")

    monkeypatch.setattr(expert_seat, "redact", redact)
    raw = "login failed:\n  the private word\t" + "x" * 400
    go(store, failing(raw))
    assert seen == [(raw, "strict")]
    assert failure(store)["reason"] == ("login failed: the [gone] word " + "x" * 400)[:300]


@pytest.mark.feature("expert-launch-failure-reason")
def test_an_error_with_no_text_is_kept_as_the_name_of_its_type(store: RedisStore) -> None:
    started(store)
    go(store, failing(""))
    assert failure(store)["reason"] == "SpawnError"


@pytest.mark.feature("expert-launch-failure-reason")
def test_a_pass_the_tick_abandons_still_leaves_the_reason_expert_status_reads(
    store: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    started(store)
    release = Event()

    class Slow(FakeRuntime):
        def spawn(self, config: Any, lane: str, name: str, task: dict[str, Any]) -> Any:
            release.wait(30)
            return super().spawn(config, lane, name, task)

    slow = Slow(crash=tick_module.SpawnError(REFUSED, "unavailable"))

    def seats(ctx: Context) -> Result:
        go(store, slow)
        return Result()

    step = FakeStep("expert seats abandoned", seats, budget_s=0.05)
    try:
        record = engine.run_step(step, Context(View(), Deadline.after(60.0)))
        assert (record.abandoned, record.result) == (True, None)
        assert failure(store) == {}
    finally:
        release.set()
        engine._RUNNING[(timing.SWARM.get(), step.name)].join(30)
    assert run("sw", "expert", "status") == 0
    assert capsys.readouterr().out.endswith(f" failed: {REFUSED})\n")


def test_no_session_slot_holds_the_launch(store: RedisStore) -> None:
    started(store)
    assert go(store, FakeRuntime(full=True)) == ["no session slot for the expert of lineage expert-1, waiting"]
    assert experts(store) == []


def test_placement_reads_the_lineage_as_the_task_it_refuses(store: RedisStore) -> None:
    asked: list[tuple[str, dict[str, str]]] = []

    class Refusing(FakeRuntime):
        def placement_refusal(self, lane: str, task: dict[str, str]) -> str:
            asked.append((lane, task))
            return "no headless hive for expert has room"

    started(store)
    runtime = Refusing()
    assert go(store, runtime) == ["no headless hive for expert has room"]
    assert asked == [("expert", {"id": "expert-1"})]
    assert runtime.spawned == [] and experts(store) == []


def test_a_full_host_holds_the_launch(store: RedisStore) -> None:
    from tests.swarm.test_host_gate import MEMORY, _decide

    started(store)
    _decide(store, 1, slug="sw", granted_at=NOW - MINUTE)
    tick_module._spend_host(store, "earlier", NOW - 1)
    held = f"holding the expert of lineage expert-1 spawn: host memory room 1, 1 spawned since it was granted: {MEMORY}"
    assert go(store, FakeRuntime()) == [held]
    assert experts(store) == []


@pytest.mark.feature("expert-seat-step")
@pytest.mark.parametrize("state", ["paused", "stopping", "stopped"])
def test_nothing_launches_while_the_swarm_is_paused_stopping_or_stopped(store: RedisStore, state: str) -> None:
    started(store)
    store.update("sw", state=state)
    runtime = FakeRuntime()
    assert go(store, runtime) == [] and runtime.spawned == []
    store.update("sw", state="running")
    assert len(go(store, runtime, NOW + MINUTE)) == 1


@pytest.mark.feature("expert-seat-step")
@pytest.mark.parametrize("state", ["running", "paused"])
def test_the_expert_of_a_stopped_lineage_is_finished_and_its_mail_closed_with_the_reason(
    store: RedisStore, state: str
) -> None:
    started(store)
    runtime = FakeRuntime()
    expert = launched(store, runtime)
    inbox = InboxStore(store.redis)
    asked = inbox.send(MASTER, expert.name, "Which batch comes first")
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "the order changed", NOW + 1)
    store.update("sw", state=state)
    assert go(store, runtime, NOW + MINUTE) == [f"ended expert {expert.name}: lineage expert-1 is stopped"]
    [ended] = experts(store)
    assert (ended.name, ended.state) == (expert.name, "finished")
    assert inbox.get(asked.id).state == "cancelled"
    [told] = inbox.inbox(MASTER)
    assert told.text.startswith(f"{expert.name} left when its lineage expert-1 was stopped before closing your message")
    left = inbox.seats.exit_of(expert.name)
    assert (left["seat"], left["reason"]) == ("", "left when its lineage expert-1 was stopped")
    assert go(store, runtime, NOW + 2 * MINUTE) == [] and len(runtime.spawned) == 1


@pytest.mark.feature("expert-seat-step")
def test_the_expert_of_a_completed_lineage_is_closed_from_the_kept_proof_and_the_master_told_once(
    store: RedisStore,
) -> None:
    lineage, runtime = started(store), FakeRuntime()
    expert = launched(store, runtime)
    expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    assert go(store, runtime, NOW + MINUTE) == [f"ended expert {expert.name}: lineage expert-1 is complete"]
    [ended] = experts(store)
    assert ended.state == "finished"
    notice = expert_seat.NOTICE.format(slug="sw", id="expert-1", title=TITLE)
    inbox = InboxStore(store.redis)
    assert [(item.sender, item.text) for item in inbox.inbox("master@sw")] == [(expert.name, notice)]
    assert [kept["text"] for kept in store.memory.recaps(lineage.seat)] == [RECAP]
    assert go(store, runtime, NOW + 2 * MINUTE) == []
    assert len(inbox.inbox("master@sw")) == 1 and len(store.memory.recaps(lineage.seat)) == 1


def test_a_completed_lineage_is_closed_even_when_the_reap_dropped_its_dead_expert_first(store: RedisStore) -> None:
    lineage, runtime = started(store), FakeRuntime()
    expert = launched(store, runtime)
    expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    store.drop_agent("sw", expert.name)
    assert go(store, runtime, NOW + MINUTE) == [f"ended expert {expert.name}: lineage expert-1 is complete"]
    notice = expert_seat.NOTICE.format(slug="sw", id="expert-1", title=TITLE)
    inbox = InboxStore(store.redis)
    assert [(item.sender, item.text) for item in inbox.inbox("master@sw")] == [(expert.name, notice)]
    assert [kept["text"] for kept in store.memory.recaps(lineage.seat)] == [RECAP]
    assert experts(store) == []
    assert go(store, runtime, NOW + 2 * MINUTE) == [] and len(inbox.inbox("master@sw")) == 1


def test_closing_a_lineage_again_writes_no_record_for_its_finished_expert(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(store)
    expert = launched(store, FakeRuntime())
    ended = expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    expert_seat.close(store, "sw", ended, PROOF)
    [finished] = experts(store)
    assert finished.state == "finished"
    written: list[str] = []
    monkeypatch.setattr(store, "put_agent", lambda slug, agent: written.append(agent.name))
    expert_seat.close(store, "sw", ended, PROOF)
    assert written == []


def test_a_lineage_whose_closing_steps_finished_leaves_nothing_for_the_pass(store: RedisStore) -> None:
    started(store)
    runtime = FakeRuntime()
    expert = launched(store, runtime)
    ended = expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    assert [row.id for row in expert_lineage.closing(store, "sw")] == ["expert-1"]
    expert_seat.close(store, "sw", ended, PROOF)
    assert expert_lineage.closing(store, "sw") == []
    assert [agent.state for agent in experts(store)] == ["finished"]
    assert go(store, runtime, NOW + MINUTE) == []


@pytest.mark.parametrize("ended", ["stopped", "complete"])
def test_the_handoff_an_ended_lineage_left_on_its_seat_is_cleared(store: RedisStore, ended: str) -> None:
    started(store)
    runtime = FakeRuntime()
    expert = launched(store, runtime)
    store.put_handoff("sw", "expert-1", HANDOFF, seat=expert.seat, envelope={"reason": "recycle"})
    if ended == "stopped":
        expert_lineage.stop(store, "sw", "expert-1", MASTER, "the order changed", NOW + 1)
    else:
        expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    go(store, runtime, NOW + MINUTE)
    assert store.handoff("sw", "expert-1") == "" and store.handoff_envelope("sw", "expert-1") is None


def test_stopping_a_lineage_clears_the_handoff_that_waited_for_its_next_expert(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started(store)
    runtime = FakeRuntime()
    handed_off(store, launched(store, runtime))
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "stop", "expert-1", "--note", "the order changed") == 0
    capsys.readouterr()
    assert store.handoff("sw", "expert-1") == ""
    assert go(store, runtime, NOW + MINUTE) == [] and len(runtime.spawned) == 1


def test_an_expert_of_a_lineage_the_swarm_does_not_hold_is_finished_and_the_other_seats_are_kept(
    store: RedisStore,
) -> None:
    started(store, at=1_000)
    runtime = FakeRuntime()
    stray = launched(store, runtime)
    store.redis.hdel(store.key("sw", "expert-lineages"), "expert-1")
    store.redis.srem(store.key("sw", "expert-running"), "expert-1")
    started(store, at=2_000)
    actions = go(store, runtime, NOW + MINUTE)
    [kept] = [agent for agent in experts(store) if agent.task == "expert-2"]
    assert actions == [
        f"ended expert {stray.name}: lineage expert-1 is unknown",
        f"spawned expert {kept.name} on lineage expert-2 as its expert number 1",
    ]
    assert [agent.state for agent in experts(store) if agent.name == stray.name] == ["finished"]


def test_a_running_id_whose_record_is_gone_is_left_out_and_the_other_seats_are_kept(store: RedisStore) -> None:
    started(store, at=1_000)
    started(store, at=2_000)
    store.redis.hdel(store.key("sw", "expert-lineages"), "expert-1")
    assert [row.id for row in expert_lineage.running(store, "sw")] == ["expert-2"]
    actions = go(store, FakeRuntime())
    [kept] = experts(store)
    assert actions == [f"spawned expert {kept.name} on lineage expert-2 as its expert number 1"]


def test_a_lineage_whose_closing_steps_fail_keeps_owing_them_and_does_not_stop_the_other_seats(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(store, at=1_000)
    runtime = FakeRuntime()
    expert = launched(store, runtime)
    expert_lineage.complete(store, "sw", "expert-1", expert.name, PROOF, NOW + 1)
    started(store, at=2_000)
    real = InboxStore.send

    def failing(*given: Any, **named: Any) -> Any:
        if given[1] == expert.name:
            raise InboxError("the inbox refused the notice")
        return real(*given, **named)

    monkeypatch.setattr(InboxStore, "send", failing)
    actions = go(store, runtime, NOW + MINUTE)
    [kept] = [agent for agent in experts(store) if agent.task == "expert-2"]
    assert actions == [
        "lineage expert-1 hit a fault this pass: the inbox refused the notice",
        f"spawned expert {kept.name} on lineage expert-2 as its expert number 1",
    ]
    assert [row.id for row in expert_lineage.closing(store, "sw")] == ["expert-1"]
    assert [agent.state for agent in experts(store) if agent.name == expert.name] == ["working"]


def test_one_pass_closes_every_completed_lineage_and_ends_the_expert_of_a_stopped_one(store: RedisStore) -> None:
    for at in (1_000, 2_000, 3_000):
        started(store, at=at)
    runtime = FakeRuntime()
    go(store, runtime)
    seated = {agent.task: agent.name for agent in experts(store)}
    for name in ("expert-1", "expert-2"):
        expert_lineage.complete(store, "sw", name, seated[name], PROOF, NOW + 1)
    expert_lineage.stop(store, "sw", "expert-3", MASTER, "the order changed", NOW + 1)
    assert go(store, runtime, NOW + MINUTE) == [
        f"ended expert {seated['expert-1']}: lineage expert-1 is complete",
        f"ended expert {seated['expert-2']}: lineage expert-2 is complete",
        f"ended expert {seated['expert-3']}: lineage expert-3 is stopped",
    ]
    assert [agent.state for agent in experts(store)] == ["finished"] * 3


def test_an_expert_whose_ending_is_refused_is_named_by_its_lineage_and_the_other_seats_are_kept(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(store, at=1_000)
    runtime = FakeRuntime()
    expert = launched(store, runtime)
    expert_lineage.stop(store, "sw", "expert-1", MASTER, "the order changed", NOW + 1)
    started(store, at=2_000)
    real = store.put_agent

    def refusing(slug: str, agent: AgentRecord) -> None:
        if agent.name == expert.name:
            raise SwarmError("the store refused the record")
        real(slug, agent)

    monkeypatch.setattr(store, "put_agent", refusing)
    actions = go(store, runtime, NOW + MINUTE)
    [kept] = [agent for agent in experts(store) if agent.task == "expert-2"]
    assert actions == [
        "lineage expert-1 hit a fault this pass: the store refused the record",
        f"spawned expert {kept.name} on lineage expert-2 as its expert number 1",
    ]
    assert [agent.state for agent in experts(store) if agent.name == expert.name] == ["working"]


def test_a_lineage_whose_launch_is_refused_does_not_stop_the_next_lineage(store: RedisStore) -> None:
    first = started(store, at=1_000)
    runtime = FakeRuntime()
    store.drop_agent("sw", launched(store, runtime).name)
    started(store, at=2_000)
    store.redis.hdel(store.key("sw", "expert-missions"), first.mission.rpartition("/")[2])
    actions = go(store, runtime, NOW + MINUTE)
    [kept] = experts(store)
    assert actions == [
        "lineage expert-1 hit a fault this pass: the saved mission of lineage expert-1 is missing or does not match "
        f"{first.mission}",
        f"spawned expert {kept.name} on lineage expert-2 as its expert number 1",
    ]


def test_an_expert_that_was_spawned_is_counted_even_when_the_pass_fails_right_after(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    started(store)
    real = RedisStore.put_agent

    def failing(*given: Any, **named: Any) -> Any:
        if given[2].lane == "expert" and given[2].state == "working":
            raise RuntimeError("the store dropped the write")
        return real(*given, **named)

    monkeypatch.setattr(RedisStore, "put_agent", failing)
    with pytest.raises(RuntimeError, match="the store dropped the write"):
        go(store, FakeRuntime())
    assert expert_lineage.launches(store, "sw", "expert-1") == 1


@pytest.mark.feature("expert-seat-step")
@pytest.mark.parametrize("recapped", [False, True])
def test_an_expert_lost_without_a_handoff_leaves_its_successor_a_recovery_handoff(
    store: RedisStore, recapped: bool
) -> None:
    lineage, runtime = started(store), FakeRuntime()
    lost = launched(store, runtime)
    if recapped:
        store.memory.add_recap(lineage.seat, lost.name, "expert-1", "## Done\nNone", NOW)
    store.drop_agent("sw", lost.name)
    actions = go(store, runtime, NOW + MINUTE)
    [successor] = experts(store)
    assert actions == [
        f"wrote the recovery handoff of lineage expert-1: {lost.name} was lost and left none",
        f"spawned expert {successor.name} on lineage expert-1 as its expert number 2",
    ]
    text = store.handoff("sw", "expert-1")
    assert handoff_expert.problems(text, Resolver("sw", store.redis), "sw", GOOD) == []
    pending = [line for line in text.splitlines() if line.startswith("- D")]
    assert [line.split()[1] for line in pending] == ["D1", "D2", "D3"] and all("unknown" in line for line in pending)
    assert all(f"unknown: {lost.name} left no account of it" in line for line in pending)
    assert f"unknown: {lost.name} held this seat and ended without a handoff" in text
    read = text.split("## Read first\n")[1].splitlines()[:-1]
    assert [line.split()[1] for line in read] == [lineage.mission, *([f"recap:{lineage.seat}"] if recapped else [])]
    assert read == [
        f"- {lineage.mission} the mission every expert of this lineage works",
        *([f"- recap:{lineage.seat} the last state an expert of this lineage handed over"] if recapped else []),
    ]
    assert store.handoff_seat("sw", "expert-1") == lineage.seat
    envelope = store.handoff_envelope("sw", "expert-1") or {}
    assert (envelope.get("reason"), envelope.get("seat"), envelope.get("agent")) == (
        expert_seat.REASON,
        lineage.seat,
        lost.name,
    )
    [row] = rows(store)
    assert (row["reason"], row["predecessor"], row["successor"], row["handoff"]) == (
        expert_seat.REASON,
        lost.name,
        successor.name,
        text,
    )
    task = runtime.tasks[-1]
    assert (task["generation"], task["handoff"], task["transfer"]["id"]) == (2, text, row["id"])
    assert go(store, runtime, NOW + 2 * MINUTE) == []


@pytest.mark.feature("expert-seat-step")
def test_an_expert_lost_before_it_confirmed_its_handoff_has_that_handoff_offered_again(store: RedisStore) -> None:
    started(store)
    runtime = FakeRuntime()
    first = handed_off(store, launched(store, runtime))
    lost = launched(store, runtime, NOW + MINUTE)
    store.drop_agent("sw", lost.name)
    actions = go(store, runtime, NOW + 2 * MINUTE)
    [third] = experts(store)
    assert actions == [
        f"offered the handoff of lineage expert-1 again: {lost.name} was lost before it confirmed it",
        f"spawned expert {third.name} on lineage expert-1 as its expert number 3",
    ]
    assert store.handoff("sw", "expert-1") == HANDOFF
    assert store.handoff_envelope("sw", "expert-1") == {"reason": "recycle"}
    offered = next(row for row in rows(store) if row["id"] != first["id"])
    assert (offered["reason"], offered["predecessor"], offered["successor"], offered["handoff"]) == (
        expert_seat.REASON,
        lost.name,
        third.name,
        HANDOFF,
    )
    assert runtime.tasks[-1]["transfer"]["id"] == offered["id"]


@pytest.mark.feature("expert-seat-step")
def test_an_expert_that_confirms_its_handoff_takes_it_off_the_seat_so_a_later_loss_is_a_recovery(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started(store)
    runtime = FakeRuntime()
    row = handed_off(store, launched(store, runtime))
    second = launched(store, runtime, NOW + MINUTE)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: SimpleNamespace(live_names=lambda: {second.name}))
    acting(monkeypatch, second.name)
    assert run("sw", "confirm-handoff", row["id"], "--next", NEXT) == 0
    assert store.handoff("sw", "expert-1") == "" and store.handoff_envelope("sw", "expert-1") is None
    assert transfers.get(store, "sw", row["id"])["continuity"]["state"] == "confirmed"
    capsys.readouterr()
    store.drop_agent("sw", second.name)
    [written, _] = go(store, runtime, NOW + 2 * MINUTE)
    assert written == f"wrote the recovery handoff of lineage expert-1: {second.name} was lost and left none"


def test_a_worker_that_confirms_its_handoff_keeps_it_stored(store: RedisStore) -> None:
    store.put_handoff("sw", "t1", V2, seat="eng-1@sw")
    expert_seat.drop_confirmed(store, "sw", AgentRecord(ENGINEER, "eng", "t1"))
    assert store.handoff("sw", "t1") == V2


@pytest.mark.feature("expert-seat-step")
def test_a_lineage_started_from_a_handoff_gives_its_first_expert_that_handoff_to_confirm(
    store: RedisStore, mission_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = mission_file.with_name("handoff.md")
    document.write_text(V2, encoding="utf-8")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "start", str(mission_file), "--handoff", str(document), "--from", SESSION) == 0
    capsys.readouterr()
    assert indexed(store) == {"expert-1"} and store.handoff("sw", "expert-1") == V2
    envelope = store.handoff_envelope("sw", "expert-1") or {}
    assert (envelope["reason"], envelope["agent"], envelope["seat"], envelope["task"]) == (
        "operator",
        SESSION,
        "expert-1@sw",
        "expert-1",
    )
    [row] = rows(store)
    assert (row["reason"], row["predecessor"], row["successor"], row["next"]) == ("operator", SESSION, "", SEED_NEXT)
    runtime = FakeRuntime()
    first = launched(store, runtime)
    task = runtime.tasks[0]
    assert (task["generation"], task["handoff"], task["transfer"]["id"]) == (1, V2, row["id"])
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: SimpleNamespace(live_names=lambda: {first.name}))
    acting(monkeypatch, first.name)
    assert run("sw", "confirm-handoff", row["id"], "--next", SEED_NEXT) == 0
    assert store.handoff("sw", "expert-1") == ""


def test_a_lineage_started_from_a_handoff_with_no_session_named_takes_its_starter_as_the_author(
    store: RedisStore, mission_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = mission_file.with_name("handoff.md")
    document.write_text(V2, encoding="utf-8")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "start", str(mission_file), "--handoff", str(document)) == 0
    capsys.readouterr()
    assert [row["predecessor"] for row in rows(store)] == [MASTER]


def test_a_handoff_document_that_does_not_conform_starts_no_lineage(
    store: RedisStore, mission_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = mission_file.with_name("handoff.md")
    document.write_text(V2.replace("## Intent\n", "## Intend\n"), encoding="utf-8")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "start", str(mission_file), "--handoff", str(document), "--from", SESSION) == 1
    assert capsys.readouterr().err == (
        "swarm: handoff refused; fix the document and run the command again:\n"
        "- ## Intend is not one of the six headings: Intent, Done, Stopped at, Decisions and promises, Next, "
        "Read first\n"
        "- ## Intent is missing\n"
    )
    assert indexed(store) == set() and store.handoff("sw", "expert-1") == "" and rows(store) == []


def test_a_handoff_document_that_cannot_be_read_starts_no_lineage(
    store: RedisStore, mission_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = mission_file.with_name("gone.md")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "start", str(mission_file), "--handoff", str(missing)) == 1
    assert capsys.readouterr().err == f"swarm: cannot read the handoff document {missing}: No such file or directory\n"
    assert indexed(store) == set()


def test_the_read_first_addresses_of_a_start_handoff_are_resolved_in_the_swarm_it_starts_in(
    store: RedisStore, mission_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = ledger_workspace.folder("sw", "t1")
    notes.mkdir(parents=True)
    (notes / "progress.md").write_text("Seam one landed.\n", encoding="utf-8")
    store.memory.add_recap("eng-1@sw", ENGINEER, "t1", "## Done\nNone", NOW)
    document = mission_file.with_name("handoff.md")
    listed = "## Read first\n- workspace:{task}/progress the notes of the session\n- recap:eng-1@sw the seat's recap"
    document.write_text(V2.replace("## Read first\nNone", listed.format(task="t1")), encoding="utf-8")
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "start", str(mission_file), "--handoff", str(document)) == 0
    capsys.readouterr()
    assert indexed(store) == {"expert-1"}
    document.write_text(V2.replace("## Read first\nNone", listed.format(task="t9")), encoding="utf-8")
    assert run("sw", "expert", "start", str(mission_file), "--handoff", str(document)) == 1
    assert "Read first entry 1: workspace:t9/progress does not resolve" in capsys.readouterr().err
    assert indexed(store) == {"expert-1"}


def test_a_session_named_without_a_handoff_document_is_refused(
    store: RedisStore, mission_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "start", str(mission_file), "--from", SESSION) == 1
    assert capsys.readouterr().err == (
        "swarm: --from names the session a handoff document comes from; give the document with --handoff\n"
    )
    assert indexed(store) == set()
