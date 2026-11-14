import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hive import account_slots
from scripts.hub import admission, slots
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_readiness import doc, snapshot, task

SLUG = "sw"


@pytest.fixture
def rig():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 2, 2))
    found = doc([task("t1"), task("t2", lane="ci", pr_url="https://example.invalid/pull/1")])
    rows = {row["id"]: row for row in found["tasks"]}
    ledger = SimpleNamespace(writes=[])

    def update_task(slug, task_id, fields, if_state=(), if_match=None):
        ledger.writes.append((slug, task_id, fields, tuple(if_state), if_match))
        row = rows[task_id]
        if row["state"] in if_state and all(
            row.get(key, "" if key == "claimed_by" else 0) == value for key, value in (if_match or {}).items()
        ):
            if fields.get("claimed_by", row.get("claimed_by", "")) != row.get("claimed_by", ""):
                row["claim_generation"] = row.get("claim_generation", 0) + 1
            row.update(fields)
        return dict(row)

    ledger.update_task = update_task
    source = SimpleNamespace(
        snapshot=lambda slug: snapshot(found, claimed=[t for t in rows if store.claimant(slug, t)])
    )
    admitter = admission.Admission(store, ledger, source, clock=lambda: 7_000)
    yield SimpleNamespace(store=store, ledger=ledger, rows=rows, admitter=admitter, found=found)
    store.redis.close()


def ask(**fields):
    return {"hive": "hive-a", "task": "t1", "lane": "eng", "generation": 0, **fields}


@pytest.mark.feature("swarm-claim-lives")
@pytest.mark.parametrize("lives", [3, 4])
def test_events_admission_blocks_spent_claim_lives_before_creating_an_execution(rig, lives, capsys):
    from scripts.gates.claims import refusal

    rig.ledger.comments = []
    rig.ledger.comment = lambda *args, **kwargs: rig.ledger.comments.append((args, kwargs))
    for _ in range(lives):
        rig.store.count_claim(SLUG, "t1")
    rig.store.put_handoff(SLUG, "t1", "handoff", envelope={"reason": "recycle"})

    assert rig.admitter.admit(SLUG, ask()) is None

    assert rig.rows["t1"]["state"] == "blocked"
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.store.claims(SLUG, "t1") == 0
    reason = refusal(lives, "recycle", "none", SLUG, "t1")
    assert rig.ledger.comments == [((SLUG, "t1", reason), {"by": "swarm"})]
    assert (
        f"hub: events claim cap swarm={SLUG} task=t1 hive=hive-a: blocked t1: {reason}"
        in capsys.readouterr().err.splitlines()
    )


@pytest.mark.feature("swarm-claim-lives")
@pytest.mark.parametrize("lives, mode", [(2, "enforce"), (3, "observe"), (3, "off")])
def test_events_admission_preserves_the_third_life_and_nonblocking_gate_modes(rig, lives, mode):
    rig.store.update(SLUG, gates={"claims": mode})
    for _ in range(lives):
        rig.store.count_claim(SLUG, "t1")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert rig.rows["t1"]["state"] == "claimed"
    assert rig.store.claims(SLUG, "t1") == lives


@pytest.mark.feature("swarm-spawn-claim-before-launch")
def test_the_hub_admits_an_offer_by_claiming_starting_seating_and_marking_it_claimed(rig):
    admitted = rig.admitter.admit(SLUG, ask())

    name = admitted["name"]
    assert rig.store.claimant(SLUG, "t1") == name
    [agent] = rig.store.agents(SLUG)
    assert (agent.name, agent.lane, agent.task, agent.seat, agent.hive) == (name, "eng", "t1", "eng-1@sw", "hive-a")
    assert (agent.state, agent.started_at) == ("starting", 7_000)
    assert rig.store.names.entry(name)["spawned_at"] == 7_000
    [occupied] = rig.store.redis.lrange(f"{rig.store.seats.key('eng-1@sw')}:history", -1, -1)
    assert json.loads(occupied)["at"] == 7_000
    from scripts.swarm import tick

    assert tick.LEASE_MS - 5_000 < rig.store.redis.pttl(rig.store.key(SLUG, "claim", "t1")) <= tick.LEASE_MS
    held = rig.store.key(SLUG, "spoke-admission", "t1", "0")
    assert admission.ADMITTED_MS - 5_000 < rig.store.redis.pttl(held) <= admission.ADMITTED_MS
    assert json.loads(rig.store.redis.get(held)) == admitted
    assert rig.store.seats.seat_of(name) == "eng-1@sw"
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("claimed", name)
    from scripts.swarm_ledger import ledger_workspace

    claimed = {"state": "claimed", "claimed_by": name, "workspace": ledger_workspace.portable(SLUG, "t1")}
    assert rig.ledger.writes == [(SLUG, "t1", claimed, ("open",), {"claimed_by": "", "claim_generation": 0})]
    assert admitted["hive"] == "hive-a"
    spawned = admitted["task"]
    assert (spawned["id"], spawned["seat"], spawned["claimed_by"]) == ("t1", "eng-1@sw", name)
    assert (spawned["execution_id"], spawned["execution_generation"], spawned["claim_generation"]) == (
        agent.execution_id,
        agent.generation,
        1,
    )
    assert spawned["claim_generation"] == rig.rows["t1"]["claim_generation"]
    json.dumps(admitted)


LAUNCH = {"harness": "codex", "profile": "engineer", "account": "work", "model": "gpt-5", "effort": "high"}


@pytest.mark.feature("events-launch-decision-record")
def test_the_hive_launch_decision_reaches_the_record_swarm_status_reads(rig):
    admitted = rig.admitter.admit(SLUG, ask())

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True

    [agent] = rig.store.agents(SLUG)
    assert agent.name == admitted["name"]
    assert (agent.harness, agent.profile, agent.account, agent.model, agent.effort) == (
        "codex",
        "engineer",
        "work",
        "gpt-5",
        "high",
    )
    assert (agent.state, agent.seat, agent.hive) == ("starting", "eng-1@sw", "hive-a")


@pytest.mark.feature("events-launch-decision-record")
def test_a_launch_decision_from_a_hive_without_the_admission_changes_no_record(rig):
    rig.admitter.admit(SLUG, ask())
    [before] = rig.store.agents(SLUG)

    assert rig.admitter.launched(SLUG, ask(hive="hive-b", launch=LAUNCH)) is False
    assert rig.admitter.launched(SLUG, ask(generation=5, launch=LAUNCH)) is False

    assert rig.store.agents(SLUG) == [before]


@pytest.mark.feature("events-launch-decision-record")
def test_a_launch_decision_for_a_seat_another_agent_took_changes_no_record(rig):
    admitted = rig.admitter.admit(SLUG, ask())
    [before] = rig.store.agents(SLUG)
    held = rig.store.key(SLUG, "spoke-admission", "t1", "0")
    rig.store.redis.set(held, json.dumps({**admitted, "name": "eng@sw-other"}))

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is False

    assert rig.store.agents(SLUG) == [before]


@pytest.mark.feature("events-launch-decision-record")
def test_a_launch_decision_after_the_agent_was_dropped_never_brings_it_back(rig):
    admitted = rig.admitter.admit(SLUG, ask())
    rig.store.drop_agent(SLUG, admitted["name"])

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is False

    assert rig.store.agents(SLUG) == []


@pytest.mark.feature("events-launch-decision-record")
def test_the_hub_answers_a_launched_request_over_the_slot_subject(rig):
    bus = open_bus(BusConfig(scoped=True))
    slots.serve(bus, admitter=rig.admitter)  # pyright: ignore[reportArgumentType]
    try:
        rig.admitter.admit(SLUG, ask())
        answered = account_slots.asker(bus.for_swarm(SLUG), SLUG, "hive-a")({"op": "launched", **ask(launch=LAUNCH)})
    finally:
        bus.close()

    assert answered is True
    [agent] = rig.store.agents(SLUG)
    assert (agent.harness, agent.profile, agent.account) == ("codex", "engineer", "work")


def test_a_task_with_a_pull_request_is_admitted_back_into_pr(rig):
    admitted = rig.admitter.admit(SLUG, ask(task="t2", lane="ci"))
    assert rig.rows["t2"]["state"] == "pr"
    assert admitted["task"]["seat"] == "ci-1@sw"


def test_a_task_returns_to_the_seat_its_handoff_names(rig):
    rig.store.put_handoff(SLUG, "t1", "carry on", seat="eng-3@sw")
    assert rig.admitter.admit(SLUG, ask())["task"]["seat"] == "eng-3@sw"


@pytest.mark.feature("hub-admission-relaunch-assignment")
def test_a_reclaimed_task_returns_to_the_seat_its_saved_launch_assignment_names(rig: SimpleNamespace) -> None:
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "a1"}
    rig.store.put_handoff(SLUG, "t1", "carry on", seat="eng-2@sw")
    rig.store.redis.hset(rig.store.key(SLUG, "launch-assignments"), "t1", json.dumps({**saved, "seat": "eng-3@sw"}))

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"]["seat"] == "eng-3@sw"
    assert admitted["task"]["launch_assignment"] == {**saved, "seat": "eng-3@sw"}


CONTINUITY_DOC = "# Handoff v2\n## Next\nRead the saved proof and push the fix.\n## Read first\nNone\n"


def left_transfer(rig: SimpleNamespace, text: str = CONTINUITY_DOC) -> dict[str, Any]:
    """The open transfer a finished predecessor of t1 left on seat eng-1 with its stored handoff."""
    from scripts.handoff import transfers
    from scripts.swarm.store import AgentRecord

    old = AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw", state="finished")
    rig.store.put_handoff(SLUG, "t1", text, seat=old.seat)
    return transfers.record(rig.store, SLUG, old, "recycle", text, 2)


@pytest.mark.feature("hub-admission-claim-continuity")
@pytest.mark.parametrize("recovery", [False, True])
def test_an_events_successor_attaches_the_transfer_its_predecessor_left(rig: SimpleNamespace, recovery: bool) -> None:
    from scripts.handoff import transfers
    from scripts.hive.remove import RECOVERY

    text = (
        RECOVERY.format(name="sw-eng-1", task="t1", hive="hive-b", wait=30, folder="/work/t1", pushed="")
        if recovery
        else CONTINUITY_DOC
    )
    transfer = left_transfer(rig, text)

    admitted = rig.admitter.admit(SLUG, ask())

    bound = transfers.get(rig.store, SLUG, transfer["id"])
    assert admitted["task"]["transfer"] == bound
    assert (bound["successor"], bound["seat"], bound["handoff"]) == (admitted["name"], "eng-1@sw", text)
    assert bound["generation"] == rig.store.seats.occupant("eng-1@sw").generation


@pytest.mark.feature("hub-admission-claim-continuity")
def test_an_events_successor_with_no_transfer_left_carries_none(rig: SimpleNamespace) -> None:
    assert rig.admitter.admit(SLUG, ask())["task"]["transfer"] is None


@pytest.mark.feature("hub-admission-claim-continuity")
def test_a_reopened_events_launch_hands_its_transfer_to_the_next_successor(rig: SimpleNamespace) -> None:
    from scripts.handoff import transfers

    transfer = left_transfer(rig)
    rig.admitter.admit(SLUG, ask())
    assert rig.admitter.reopen(SLUG, ask()) is True
    detached = transfers.get(rig.store, SLUG, transfer["id"])

    admitted = rig.admitter.admit(SLUG, ask(generation=rig.rows["t1"]["claim_generation"]))

    assert (detached["successor"], detached["binding"]) == ("", {"state": "pending"})
    assert [row["id"] for row in transfers.list_transfers(rig.store, SLUG)] == [transfer["id"]]
    assert admitted["task"]["transfer"]["id"] == transfer["id"]
    assert transfers.get(rig.store, SLUG, transfer["id"])["successor"] == admitted["name"]


@pytest.mark.feature("hub-admission-claim-continuity")
def test_a_repeated_reopen_leaves_the_detached_transfer_as_it_was(rig: SimpleNamespace) -> None:
    from scripts.handoff import transfers

    left_transfer(rig)
    rig.admitter.admit(SLUG, ask())
    assert rig.admitter.reopen(SLUG, ask()) is True
    once = transfers.list_transfers(rig.store, SLUG)

    assert rig.admitter.reopen(SLUG, ask()) is False

    assert transfers.list_transfers(rig.store, SLUG) == once
    assert [row["successor"] for row in once] == [""]


@pytest.mark.feature("hub-admission-claim-continuity")
def test_a_late_events_admission_hands_its_transfer_back(rig: SimpleNamespace) -> None:
    from scripts.handoff import transfers

    transfer = left_transfer(rig)
    rig.admitter.clock = late_clock(5)

    with pytest.raises(admission.LateAdmission):
        rig.admitter.admit(SLUG, ask())

    assert transfers.get(rig.store, SLUG, transfer["id"])["successor"] == ""
    assert rig.store.claimant(SLUG, "t1") is None


@pytest.mark.feature("hub-admission-claim-continuity")
def test_a_refused_transfer_attach_releases_the_claim_the_agent_and_the_ledger_row(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.handoff import transfers
    from scripts.swarm.store import SwarmError

    def refuse(store: object, slug: str, agent: object) -> None:
        raise SwarmError("The transfer changed; retry the command")

    left_transfer(rig)
    monkeypatch.setattr(transfers, "attach", refuse)

    with pytest.raises(SwarmError, match="^The transfer changed"):
        rig.admitter.admit(SLUG, ask())

    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("open", "")
    assert rig.store.redis.get(rig.admitter._key(SLUG, ask())) is None


@pytest.mark.feature("swarm-tick-successor-fill")
def test_a_hive_successor_from_a_saved_launch_with_neither_field_is_admitted_on_the_lane_model_inside_the_range(
    rig: SimpleNamespace,
) -> None:
    rig.store.update(SLUG, lanes={"eng": {"model": "sonnet-lane"}})
    launch = {"profile": "engineer", "harness": "claude", "model": "", "effort": "", "account": "a1"}
    rig.store.put_handoff(SLUG, "t1", "handoff", envelope={"reason": "context", "launch": launch})

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"]["handoff_envelope"]["launch"] == {**launch, "model": "sonnet-lane", "effort": "medium"}


def test_a_task_whose_dependencies_are_done_is_admitted_with_an_empty_stack_base(rig):
    assert rig.admitter.admit(SLUG, ask())["task"]["stack_base"] == []


@pytest.mark.parametrize(
    "branch_repo, stacked",
    [
        ("https://github.com/acme/app.git", {"task": "t2", "branch": "feat-two"}),
        (
            "https://github.com/acme/other",
            {"task": "t2", "branch": "feat-two", "repo": "https://github.com/acme/other"},
        ),
    ],
)
def test_a_task_on_an_open_dependency_stacks_on_its_branch_by_the_stored_origin(rig, branch_repo, stacked):
    rig.store.update(SLUG, repo_origin="github.com/acme/app")
    rig.rows["t2"].update(state="pr", branch="feat-two", branch_repo=branch_repo)
    rig.rows["t1"]["depends_on"] = ["t2"]

    assert rig.admitter.admit(SLUG, ask())["task"]["stack_base"] == [stacked]


def test_a_swarm_without_a_stored_origin_compares_with_its_checkout_origin(rig, monkeypatch):
    from scripts.swarm import stack

    monkeypatch.setattr(stack, "origin_key", lambda repo: {"/repo": "github.com/acme/app"}.get(repo, ""))
    rig.rows["t2"].update(state="pr", branch="feat-two", branch_repo="git@github.com:acme/app.git")
    rig.rows["t1"]["depends_on"] = ["t2"]

    assert rig.admitter.admit(SLUG, ask())["task"]["stack_base"] == [{"task": "t2", "branch": "feat-two"}]


def test_the_claim_is_guarded_on_the_claimant_the_offer_saw(rig):
    rig.rows["t1"]["claimed_by"] = "earlier"
    name = rig.admitter.admit(SLUG, ask())["name"]
    assert rig.ledger.writes[0][4] == {"claimed_by": "earlier", "claim_generation": 0}
    assert rig.rows["t1"]["claimed_by"] == name


def test_a_default_admission_reads_through_the_service_ledger_and_the_given_store(rig):
    admitter = admission.Admission(rig.store)
    assert admitter.ledger.service is True
    assert (admitter.source._store, admitter.source._ledger) == (rig.store, admitter.ledger)
    assert admitter.store is rig.store


def test_a_repeat_from_the_same_hive_returns_its_admission_and_another_hive_gets_none(rig):
    first = rig.admitter.admit(SLUG, ask())
    assert rig.admitter.admit(SLUG, ask()) == first
    assert rig.admitter.admit(SLUG, ask(hive="hive-b")) is None
    assert len(rig.store.agents(SLUG)) == 1
    assert len(rig.ledger.writes) == 1


@pytest.mark.parametrize(
    "fields",
    [{"lane": "ci"}, {"generation": 1}, {"task": "gone"}],
)
def test_an_offer_the_ledger_no_longer_backs_is_withdrawn_without_a_claim(rig, fields):
    assert rig.admitter.admit(SLUG, ask(**fields)) is None
    assert rig.store.agents(SLUG) == []
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.ledger.writes == []


@pytest.mark.feature("swarm-spawn-claim-before-launch")
def test_a_ledger_that_refuses_the_claim_releases_the_claim_and_the_agent(rig):
    rig.ledger.update_task = lambda *args, **kwargs: {**rig.rows["t1"], "state": "claimed", "claimed_by": "other"}
    assert rig.admitter.admit(SLUG, ask()) is None
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []


@pytest.mark.feature("swarm-spawn-claim-before-launch")
def test_a_ledger_outage_during_the_claim_releases_the_claim_and_the_agent(rig):
    def down(*args, **kwargs):
        raise ConnectionError("ledger down")

    rig.ledger.update_task = down
    with pytest.raises(ConnectionError, match="^ledger down$"):
        rig.admitter.admit(SLUG, ask())
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []


def test_a_seat_whose_earlier_execution_ended_is_admitted_at_the_next_generation(rig):
    first = rig.admitter.admit(SLUG, ask())
    earlier = rig.store.execution(SLUG, first["task"]["execution_id"])
    assert rig.admitter.reopen(SLUG, ask()) is True

    admitted = rig.admitter.admit(SLUG, ask(generation=rig.rows["t1"]["claim_generation"]))

    [agent] = rig.store.agents(SLUG)
    assert (agent.name, agent.seat) == (admitted["name"], "eng-1@sw")
    assert agent.generation == earlier.generation + 1 == 2
    assert agent.execution_id != earlier.execution_id
    assert rig.store.execution_occupants(SLUG)["eng-1@sw"].execution_id == agent.execution_id
    assert (admitted["task"]["execution_id"], admitted["task"]["execution_generation"]) == (agent.execution_id, 2)


def test_a_refused_execution_start_releases_the_claim_and_the_agent(rig, monkeypatch):
    from scripts.swarm.store import SwarmError

    def refuse(slug, agent, previous_execution_id=""):
        raise SwarmError("refused start")

    monkeypatch.setattr(rig.store, "start_execution", refuse)
    with pytest.raises(SwarmError, match="^refused start$"):
        rig.admitter.admit(SLUG, ask())
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []


def test_reopen_returns_an_admitted_task_to_open_and_forgets_its_admission(rig):
    name = rig.admitter.admit(SLUG, ask())["name"]

    assert rig.admitter.reopen(SLUG, ask(hive="hive-b")) is False
    assert rig.store.claimant(SLUG, "t1") == name
    assert rig.admitter.reopen(SLUG, ask()) is True

    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("open", "")
    assert rig.ledger.writes[-1] == (
        SLUG,
        "t1",
        {"state": "open", "claimed_by": ""},
        ("claimed", "pr"),
        {"claimed_by": name},
    )
    assert rig.admitter.reopen(SLUG, ask()) is False


@pytest.mark.feature("events-failed-launch-releases-admission")
def test_an_admission_finished_after_the_hive_stopped_waiting_is_released_and_refused(rig: SimpleNamespace) -> None:
    rig.admitter.clock = late_clock(0)

    with pytest.raises(admission.LateAdmission, match="t1"):
        rig.admitter.admit(SLUG, ask())

    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("open", "")
    assert rig.store.redis.get(rig.admitter._key(SLUG, ask())) is None


@pytest.mark.feature("events-failed-launch-releases-admission")
def test_an_admission_finished_just_inside_the_hive_wait_is_kept(rig: SimpleNamespace) -> None:
    rig.admitter.clock = late_clock(-1)

    name = rig.admitter.admit(SLUG, ask())["name"]

    assert rig.store.claimant(SLUG, "t1") == name
    assert rig.rows["t1"]["state"] == "claimed"


@pytest.mark.feature("events-admission-saved-fleet-red-listing")
def test_an_admission_of_a_task_with_a_pull_request_answers_inside_the_wait_while_a_live_listing_is_slow(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import fleet_red, readiness

    now = [7_000]

    def slow(repo: str) -> list[object]:
        now[0] += 22_000
        return []

    monkeypatch.setattr(fleet_red, "read", slow)
    rig.rows["t2"]["pr_url"] = "https://github.com/o/r/pull/1"
    rig.ledger.metadata = lambda slug: {"_meta": {"rev": 1}}
    rig.ledger.state = lambda slug: doc(list(rig.rows.values()))
    source = readiness.Source(rig.store, rig.ledger)
    admitter = admission.Admission(rig.store, rig.ledger, source, clock=lambda: now[0])

    admitted = admitter.admit(SLUG, ask(task="t2", lane="ci"))

    assert admitted is not None and admitted["task"]["seat"] == "ci-1@sw"
    assert now == [7_000]


def late_clock(offset: int) -> Callable[[], int]:
    readings: list[None] = []

    def clock() -> int:
        readings.append(None)
        return 7_000 if len(readings) == 1 else 7_000 + admission.ANSWER_MS + offset

    return clock


@pytest.mark.feature("hub-offer-logging")
def test_a_late_admission_prints_one_line_naming_the_member_the_task_and_the_wait(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.admitter.clock = late_clock(5)

    with pytest.raises(admission.LateAdmission):
        rig.admitter.admit(SLUG, ask())

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a late: finished after {admission.ANSWER_MS + 5} ms, "
        f"the hive stopped waiting at {admission.ANSWER_MS} ms"
    ]


@pytest.mark.feature("hub-offer-logging")
def test_an_admission_held_by_another_hive_prints_one_refusal_line(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.admitter.admit(SLUG, ask())
    capsys.readouterr()

    assert rig.admitter.admit(SLUG, ask(hive="hive-b")) is None

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-b refused: already admitted to hive hive-a"
    ]


@pytest.mark.feature("hub-offer-logging")
def test_an_admission_while_claims_are_stopped_prints_one_refusal_line(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(admission.run_window, "claims_stopped", lambda *_: True)

    assert rig.admitter.admit(SLUG, ask()) is None

    assert rig.store.claimant(SLUG, "t1") is None
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: claims are stopped"
    ]


@pytest.mark.feature("hub-offer-logging")
def test_an_admission_for_a_withdrawn_offer_prints_its_withdrawal_reason(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.admitter.admit(SLUG, ask(generation=1)) is None

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: the offer is withdrawn, it is at generation 0"
    ]


@pytest.mark.feature("hub-offer-logging")
def test_an_admission_whose_claim_another_holder_took_prints_one_refusal_line(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.ledger.update_task = lambda *args, **kwargs: {**rig.rows["t1"], "state": "claimed", "claimed_by": "other"}

    assert rig.admitter.admit(SLUG, ask()) is None

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: another holder took the claim"
    ]


@pytest.mark.feature("hub-offer-logging")
def test_an_admitted_offer_prints_no_refusal_line(rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert rig.admitter.admit(SLUG, ask()) is not None

    assert capsys.readouterr().err == ""


def test_the_hub_answers_inside_the_window_the_hive_waits_for_an_admission() -> None:
    assert 0 < admission.ANSWER_MS < account_slots.REQUEST_TIMEOUT_MS


def test_an_admission_carries_no_avoided_harness_until_a_launch_fails_on_one(rig):
    assert "avoid_harness" not in rig.admitter.admit(SLUG, ask())["task"]


def test_a_task_whose_last_launch_failed_on_a_harness_is_admitted_steered_off_it(rig):
    rig.store.note_launch_failure(SLUG, "t1", "launch failed", "codex")
    assert rig.admitter.admit(SLUG, ask())["task"]["avoid_harness"] == "codex"


def test_a_failed_launch_records_its_harness_and_steers_the_redelivered_offer(rig, capsys):
    first = rig.admitter.admit(SLUG, ask())
    held = rig.store.key(SLUG, "spoke-admission", "t1", "0")
    capsys.readouterr()

    assert rig.admitter.failed(SLUG, ask(harness="codex", reason="launch failed")) is True

    assert rig.store.failed_harness(SLUG, "t1") == "codex"
    assert rig.store.launch_failure(SLUG, "t1") == "launch failed"
    again = rig.admitter.admit(SLUG, ask())
    assert again == {**first, "task": {**first["task"], "avoid_harness": "codex"}}
    assert admission.ADMITTED_MS - 5_000 < rig.store.redis.pttl(held) <= admission.ADMITTED_MS
    assert capsys.readouterr() == ("", f"hub: events steering swarm={SLUG} task=t1 avoid=codex: launch failed\n")


def test_a_failed_launch_naming_no_harness_records_the_failure_and_steers_nothing(rig, capsys):
    first = rig.admitter.admit(SLUG, ask())
    capsys.readouterr()

    assert rig.admitter.failed(SLUG, ask(harness="", reason="launch failed")) is True

    assert rig.store.launch_failure(SLUG, "t1") == "launch failed"
    assert rig.store.failed_harness(SLUG, "t1") == ""
    assert rig.admitter.admit(SLUG, ask()) == first
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("fields", [{"hive": "hive-b"}, {"generation": 1}])
def test_a_failure_report_without_its_admission_records_nothing(rig, fields):
    first = rig.admitter.admit(SLUG, ask())

    assert rig.admitter.failed(SLUG, ask(harness="codex", reason="launch failed", **fields)) is False

    assert rig.store.failed_harness(SLUG, "t1") == ""
    assert rig.store.launch_failure(SLUG, "t1") == ""
    assert rig.admitter.admit(SLUG, ask()) == first


def test_the_slot_service_routes_failure_reports_to_admission():
    assert admission.OPERATIONS == ("admit", "reopen", "failed", "launched")


def test_the_slot_service_answers_admission_for_the_swarm_and_hive_of_its_subject():
    bus = open_bus(BusConfig(scoped=True))
    seen = []

    class Admitter:
        def admit(self, slug, request):
            seen.append(("admit", slug, request))
            return {"name": "eng@x"}

        def reopen(self, slug, request):
            seen.append(("reopen", slug, request))
            return True

    slots.serve(bus, admitter=Admitter())
    member = account_slots.asker(bus.for_swarm(SLUG), SLUG, "hive-a")
    assert member({"op": "admit", **ask()}) == {"name": "eng@x"}
    assert member({"op": "reopen", **ask()}) is True
    assert seen == [("admit", SLUG, {"op": "admit", **ask()}), ("reopen", SLUG, {"op": "reopen", **ask()})]
    with pytest.raises(account_slots.Unavailable, match="bound to hive 'hive-a'"):
        member({"op": "admit", **ask(hive="hive-b")})
    assert len(seen) == 2
    bus.close()


@pytest.mark.feature("hive-drain-events-admission")
def test_the_hub_admits_a_launch_for_a_hive_that_is_not_draining(rig, monkeypatch, capsys):
    from scripts.hive import registry

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    registry.beat(rig.store.redis, "hive-a", {"heartbeat_at": 6_000})
    registry.update(rig.store.redis, "hive-a", ["draining=no"])

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert capsys.readouterr().err == ""


@pytest.mark.feature("hive-drain-events-admission")
def test_the_hub_admits_no_launch_for_a_hive_draining_for_a_release(rig, monkeypatch, capsys):
    from scripts.hive import registry

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    registry.beat(rig.store.redis, "hive-a", {"heartbeat_at": 6_000})
    registry.update(rig.store.redis, "hive-a", ["draining=yes"])

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is None
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []
    assert rig.rows["t1"]["state"] == "open"
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events placement swarm={SLUG} task=t1 hive=hive-a: refused, the hive is draining"
    ]


def _hive_holding(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, cap: str, live: int) -> None:
    from dataclasses import replace

    from scripts.hive import registry
    from scripts.swarm.store import AgentRecord

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    registry.beat(rig.store.redis, "hive-a", {"heartbeat_at": 6_000})
    if cap:
        registry.update(rig.store.redis, "hive-a", [f"max-agents={cap}"])
    rig.store.create(SwarmConfig("other", "/repo", 2, 2))
    held = [("other", "eng", "eng-1", "working", "hive-a"), (SLUG, "ci", "ci-1", "starting", "hive-a")][:live]
    ignored = [
        ("other", "eng", "eng-2", "finished", "hive-a"),
        ("other", "ci", "ci-1", "ended", "hive-a"),
        (SLUG, "master", "master", "working", "hive-a"),
        ("other", "ci", "ci-2", "working", "hive-b"),
    ]
    for slug, lane, seat, state, hive in [*held, *ignored]:
        record = AgentRecord(rig.store.next_name(slug, lane), lane, "", seat=f"{seat}@{slug}", started_at=1, hive=hive)
        agent = rig.store.start_execution(slug, record)
        rig.store.put_agent(slug, replace(agent, state=state))


@pytest.mark.feature("hub-admission-hive-max-agents")
@pytest.mark.parametrize("cap, live", [("2", 2), ("0", 0)])
def test_the_hub_admits_no_launch_for_a_hive_at_its_max_agents(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cap: str, live: int
) -> None:
    _hive_holding(rig, monkeypatch, cap, live)
    before = {slug: len(rig.store.agents(slug)) for slug in (SLUG, "other")}

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted == {"held": f"{live} of {cap} agents on the hive"}
    assert rig.store.claimant(SLUG, "t1") is None
    assert {slug: len(rig.store.agents(slug)) for slug in (SLUG, "other")} == before
    assert rig.ledger.writes == []
    assert rig.rows["t1"]["state"] == "open"
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: {live} of {cap} agents on the hive"
    ]


@pytest.mark.feature("hub-admission-hive-max-agents")
@pytest.mark.parametrize("hive, held", [("root", True), ("hive-a", False)])
def test_only_the_root_hive_counts_workers_that_name_no_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, hive: str, held: bool
) -> None:
    from scripts.hive import registry
    from scripts.swarm import readiness
    from scripts.swarm.store import AgentRecord

    named = readiness.ROOT if hive == "root" else hive
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    registry.beat(rig.store.redis, named, {"heartbeat_at": 6_000})
    registry.update(rig.store.redis, named, ["max-agents=1"])
    record = AgentRecord(rig.store.next_name(SLUG, "ci"), "ci", "t2", seat=f"ci-1@{SLUG}", started_at=1)
    rig.store.start_execution(SLUG, record)

    admitted = rig.admitter.admit(SLUG, ask(hive=named))

    assert admitted is not None
    assert ("held" in admitted) is held


@pytest.mark.feature("hub-admission-hive-max-agents")
def test_two_hub_pods_on_one_store_never_admit_a_hive_past_its_max_agents(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import registry

    _hive_holding(rig, monkeypatch, "2", 1)
    monkeypatch.setattr(admission, "TURN_TRIES", 1)
    monkeypatch.setattr(admission, "TURN_WAIT_S", 0)
    other = admission.Admission(rig.store, rig.ledger, rig.admitter.source, clock=lambda: 7_000)
    claim = rig.admitter._claim
    raced: list[dict[str, Any] | None] = []

    def racing(*args: Any) -> dict[str, Any] | None:
        raced.append(other.admit(SLUG, ask(task="t2", lane="ci")))
        return claim(*args)

    monkeypatch.setattr(rig.admitter, "_claim", racing)

    first = rig.admitter.admit(SLUG, ask())

    assert first is not None
    assert first["hive"] == "hive-a"
    assert raced == [{"held": admission.BUSY}]
    assert rig.store.claimant(SLUG, "t2") is None
    assert other.admit(SLUG, ask(task="t2", lane="ci")) == {"held": "2 of 2 agents on the hive"}
    assert rig.store.redis.get(f"{registry.key('hive-a')}:admitting") is None


@pytest.mark.feature("hub-admission-hive-max-agents")
def test_the_hive_admitting_key_expires_and_a_held_key_holds_only_its_own_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import registry

    _hive_holding(rig, monkeypatch, "2", 0)
    monkeypatch.setattr(admission, "TURN_TRIES", 1)
    monkeypatch.setattr(admission, "TURN_WAIT_S", 0)
    claim = rig.admitter._claim
    expiries: list[int] = []

    def watched(*args: Any) -> dict[str, Any] | None:
        expiries.append(rig.store.redis.pttl(f"{registry.key('hive-a')}:admitting"))
        return claim(*args)

    monkeypatch.setattr(rig.admitter, "_claim", watched)
    rig.store.redis.set(f"{registry.key('hive-a')}:admitting", "another pod")

    assert rig.admitter.admit(SLUG, ask()) == {"held": admission.BUSY}
    assert rig.store.redis.get(f"{registry.key('hive-a')}:admitting") == "another pod"

    rig.store.redis.delete(f"{registry.key('hive-a')}:admitting")
    rig.store.redis.set(f"{registry.key('hive-b')}:admitting", "another pod")
    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert len(expiries) == 1
    assert 0 < expiries[0] <= admission.TURN_MS


@pytest.mark.feature("hub-admission-hive-max-agents")
@pytest.mark.parametrize("cap, live", [("2", 1), ("3", 2), ("", 2)])
def test_the_hub_admits_a_launch_for_a_hive_below_its_max_agents(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cap: str, live: int
) -> None:
    _hive_holding(rig, monkeypatch, cap, live)

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert capsys.readouterr().err == ""


def _released(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, member: str, hub: str) -> None:
    from scripts import updater
    from scripts.hive import registry

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setattr(updater, "installed_version", lambda: hub)
    registry.beat(rig.store.redis, "hive-a", {"heartbeat_at": 6_000, "version": member})


@pytest.mark.feature("hub-admission-release-floor")
@pytest.mark.parametrize(
    "member, hub",
    [
        ("2.17.825", "2.17.843"),
        ("2.9.0", "2.17.0"),
        ("2.17.825.dev4+g1a2b", "2.17.826"),
        ("2", "2.17.843"),
        ("2.17.826.dev1", "2.17.826"),
        ("2.17.826rc1", "2.17.826"),
        ("2.17.826a1", "2.17.826"),
        ("2.17.826-rc1", "2.17.826"),
        ("v2.9.0", "2.17.0"),
    ],
)
def test_the_hub_admits_no_launch_for_a_hive_on_an_older_release(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], member: str, hub: str
) -> None:
    _released(rig, monkeypatch, member, hub)

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is None
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []
    assert rig.rows["t1"]["state"] == "open"
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: release {member} below hub release {hub}"
    ]


@pytest.mark.feature("hub-admission-release-floor")
def test_the_hub_refuses_a_held_admission_redelivered_to_a_hive_on_an_older_release(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _released(rig, monkeypatch, "", "2.17.843")
    assert rig.admitter.admit(SLUG, ask()) is not None
    writes = list(rig.ledger.writes)
    capsys.readouterr()
    _released(rig, monkeypatch, "2.17.825", "2.17.843")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is None
    assert rig.ledger.writes == writes
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: release 2.17.825 below hub release 2.17.843"
    ]


@pytest.mark.feature("hub-admission-release-floor")
@pytest.mark.parametrize(
    "member, hub",
    [
        ("2.17.843", "2.17.843"),
        ("2.17.844", "2.17.843"),
        ("2.18.0", "2.17.843"),
        ("", "2.17.843"),
        ("unknown", "2.17.843"),
        ("2.17.0", "2.17"),
        ("2.17.826+g1a2b", "2.17.826"),
        ("2.17.826.post1", "2.17.826"),
        ("2.17.827.dev1", "2.17.826"),
        ("2.17.825", "unknown"),
    ],
)
def test_the_hub_admits_a_launch_for_a_hive_on_the_hub_release_or_an_unreported_one(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], member: str, hub: str
) -> None:
    _released(rig, monkeypatch, member, hub)

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert capsys.readouterr().err == ""


@pytest.mark.feature("hub-admission-release-floor")
def test_the_hub_admits_a_launch_for_a_hive_with_no_registry_record(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import updater

    monkeypatch.setattr(updater, "installed_version", lambda: "2.17.843")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert capsys.readouterr().err == ""


@pytest.mark.feature("hub-admission-carries-ledger-intent")
def test_an_admitted_engineer_classifies_its_profile_on_the_hive_without_a_ledger_read(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import ledger_read, profile_choice
    from scripts.swarm.store import SwarmError

    def refused(slug: str, *keys: str) -> dict:
        raise SwarmError(f"the hosted ledger is unavailable for {slug}: a remote ledger client needs a pinned identity")

    monkeypatch.setattr(ledger_read, "parts", refused)

    admitted = rig.admitter.admit(SLUG, ask())

    picked = profile_choice.state(SLUG, admitted["task"])
    assert (picked["task"], picked["project_intent"], picked["phase"], picked["phase_intent"]) == (
        "t1",
        "o",
        "p1",
        "build: ",
    )
    assert admitted["task"][profile_choice.LEDGER_INTENT] == {
        "overview": "o",
        "phases": [{"id": "p1", "title": "build", "depends_on": []}],
    }
    json.dumps(admitted)


def _github_heads(monkeypatch: pytest.MonkeyPatch, found: dict[str, Any]) -> list[dict[str, str]]:
    from scripts.swarm import merge_queue

    asked: list[dict[str, str]] = []

    def graphql(query: str, variables: dict[str, str], run: Any = None, timeout: float = 0) -> dict[str, Any]:
        asked.append(variables)
        return {"repository": found}

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    return asked


@pytest.mark.feature("hub-admission-claim-context")
def test_a_reclaimed_task_continues_from_its_recorded_branch_read_through_github(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.store.update(SLUG, repo_origin="github.com/acme/app")
    rig.rows["t1"]["branch"] = "engineer-old"
    asked = _github_heads(monkeypatch, {"b0": {"target": {"oid": "abc123"}}})

    admitted = rig.admitter.admit(SLUG, ask())

    expected = {
        "remote_branch": "engineer-old",
        "remote_head": "abc123",
        "continue_from": "origin/engineer-old",
        "fresh_reason": "none",
    }
    assert admitted["task"]["reclaim"] == expected
    assert rig.store.reclaims(SLUG) == {admitted["name"]: expected}
    assert asked == [{"owner": "acme", "name": "app", "b0": "refs/heads/engineer-old"}]
    json.dumps(admitted)


@pytest.mark.feature("hub-admission-claim-context")
def test_a_handed_off_task_carries_its_handoff_and_resolved_envelope(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.store.update(SLUG, repo_origin="github.com/acme/app")
    rig.rows["t1"]["branch"] = "engineer-old"
    rig.store.put_handoff(SLUG, "t1", "handoff text", envelope={"reason": "recycle"})
    _github_heads(monkeypatch, {"b0": {"target": {"oid": "abc123"}}})

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"]["handoff"] == "handoff text"
    assert admitted["task"]["handoff_envelope"] == {
        "reason": "recycle",
        "remote_branch": "engineer-old",
        "remote_head": "abc123",
        "continue_from": "origin/engineer-old",
        "fresh_reason": "none",
    }
    assert "reclaim" not in admitted["task"]
    assert rig.store.reclaims(SLUG) == {}


@pytest.mark.feature("hub-admission-claim-context")
def test_the_claim_write_names_a_portable_workspace_clears_a_protected_branch_and_sets_the_lane_kind(
    rig: SimpleNamespace,
) -> None:
    from scripts.hive import orders
    from scripts.swarm_ledger import ledger_workspace

    rig.store.update(SLUG, lanes={"eng": {"kind": "ops"}})
    rig.rows["t1"]["branch"] = "dev"

    admitted = rig.admitter.admit(SLUG, ask())

    portable = ledger_workspace.portable(SLUG, "t1")
    [(_, _, fields, _, _)] = rig.ledger.writes
    assert fields == {
        "state": "claimed",
        "claimed_by": admitted["name"],
        "workspace": portable,
        "branch": "",
        "kind": "ops",
    }
    assert not ledger_workspace.folder(SLUG, "t1").exists()
    assert (admitted["task"]["workspace"], admitted["task"]["branch"], admitted["task"]["kind"]) == (
        portable,
        "",
        "ops",
    )
    assert admitted["task"][orders.SEEDS] == {"steering.md": "# t1: title t1\n"}
    assert "reclaim" not in admitted["task"]


@pytest.mark.feature("hub-admission-claim-context")
def test_the_claim_write_keeps_a_real_branch_and_a_task_kind(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.store.update(SLUG, repo_origin="github.com/acme/app", lanes={"eng": {"kind": "ops"}})
    rig.rows["t1"].update(branch="engineer-old", kind="research")
    _github_heads(monkeypatch, {"b0": None})

    admitted = rig.admitter.admit(SLUG, ask())

    [(_, _, fields, _, _)] = rig.ledger.writes
    assert set(fields) == {"state", "claimed_by", "workspace"}
    assert admitted["task"]["reclaim"]["continue_from"] == "fresh"


@pytest.mark.feature("hub-admission-claim-context")
def test_an_auto_lane_kind_writes_no_kind(rig: SimpleNamespace) -> None:
    rig.store.update(SLUG, lanes={"eng": {"kind": "auto"}})

    rig.admitter.admit(SLUG, ask())

    [(_, _, fields, _, _)] = rig.ledger.writes
    assert set(fields) == {"state", "claimed_by", "workspace"}


@pytest.mark.feature("hub-admission-claim-context")
def test_an_admitted_task_carries_its_territory_overlaps_and_its_group(rig: SimpleNamespace) -> None:
    rig.rows["t1"].update(territory=["scripts/hub"], group_members=["t2", "gone"])
    rig.rows["t2"].update(state="pr", territory=["scripts/hub/admission.py"], description="the ci task")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"]["overlaps"] == [
        {"task": "t2", "claimant": "", "areas": ["scripts/hub/admission.py"], "checks": True}
    ]
    assert admitted["task"]["group"] == [{"id": "t2", "title": "title t2", "description": "the ci task"}]
    json.dumps(admitted)


@pytest.mark.feature("hub-admission-claim-context")
def test_the_seeds_carry_the_folder_the_previous_agents_hive_synced(rig: SimpleNamespace) -> None:
    from scripts.hive import orders
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_ledger import ledger_workspace

    named = rig.store.next_name(SLUG, "eng", 1)
    earlier = AgentRecord(named, "eng", "t1", seat="eng-9@sw", state="starting", started_at=1, hive="hive-b")
    rig.store.start_execution(SLUG, earlier, "")
    synced = {SLUG: {"t1": {"progress.md": "- earlier step\n", "steering.md": "stale"}}}
    rig.store.redis.set(ledger_workspace.files_key("hive-b"), json.dumps(synced))

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"][orders.SEEDS] == {"progress.md": "- earlier step\n", "steering.md": "# t1: title t1\n"}


@pytest.mark.feature("hub-admission-claim-context")
def test_a_work_folder_refusal_releases_the_claim_and_the_agent(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_ledger import ledger_workspace

    def refused(slug: str, task_id: str) -> str:
        raise ValueError("work folder needs a plain slug and task id")

    monkeypatch.setattr(ledger_workspace, "portable", refused)

    with pytest.raises(ValueError, match="work folder needs a plain slug and task id"):
        rig.admitter.admit(SLUG, ask())

    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []


@pytest.mark.feature("hub-admission-claim-context")
def test_a_first_life_with_no_branch_reads_no_branch_heads(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hub import branch_heads

    def unread(config: SwarmConfig) -> None:
        raise AssertionError("a first life with no branch needs no reclaim")

    monkeypatch.setattr(branch_heads, "reclaimer", unread)

    admitted = rig.admitter.admit(SLUG, ask())

    assert "reclaim" not in admitted["task"]
    assert rig.store.reclaims(SLUG) == {}


@pytest.mark.feature("hub-admission-claim-context")
def test_a_cleared_protected_branch_continues_from_the_earlier_life_instead(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.store.update(SLUG, repo_origin="github.com/acme/app")
    rig.rows["t1"]["branch"] = "dev"
    life = {"task": "t1", "lane": "eng", "name": "eng@sw-0000", "ended_at": 1}
    rig.store.redis.rpush(rig.store.key(SLUG, "history"), json.dumps(life))
    asked = _github_heads(monkeypatch, {"b0": {"target": {"oid": "abc123"}}})

    admitted = rig.admitter.admit(SLUG, ask())

    assert asked == [{"owner": "acme", "name": "app", "b0": "refs/heads/eng-sw-0000"}]
    assert admitted["task"]["reclaim"]["continue_from"] == "origin/eng-sw-0000"


@pytest.mark.feature("hub-admission-claim-context")
@pytest.mark.parametrize("handed_off", [False, True])
def test_a_branch_another_pull_request_in_the_stored_origin_holds_is_never_continued(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, handed_off: bool
) -> None:
    from scripts.swarm import stack

    monkeypatch.setattr(stack, "origin_key", lambda repo: pytest.fail("a stored origin needs no checkout"))
    rig.store.update(SLUG, repo_origin="github.com/acme/app")
    life = {"task": "t1", "lane": "eng", "name": "eng@sw-0000", "ended_at": 1}
    rig.store.redis.rpush(rig.store.key(SLUG, "history"), json.dumps(life))
    rig.rows["t2"].update(branch="eng-sw-0000", pr_url="https://github.com/acme/app/pull/2")
    if handed_off:
        rig.store.put_handoff(SLUG, "t1", "handoff text", envelope={"reason": "recycle"})
    _github_heads(monkeypatch, {"b0": {"target": {"oid": "abc123"}}})

    admitted = rig.admitter.admit(SLUG, ask())

    verdict = admitted["task"]["handoff_envelope" if handed_off else "reclaim"]
    assert (verdict["continue_from"], verdict["fresh_reason"]) == (
        "fresh",
        "no branch is recorded for this task outside another task's pull request",
    )


@pytest.mark.feature("hub-admission-claim-context")
def test_the_seeds_follow_the_most_recently_started_earlier_agent(rig: SimpleNamespace) -> None:
    from scripts.hive import orders
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_ledger import ledger_workspace

    for started, hive in ((1, "hive-c"), (5, "hive-b")):
        named = rig.store.next_name(SLUG, "eng", started)
        record = AgentRecord(named, "eng", "t1", seat=f"eng-{started + 7}@sw", started_at=started, hive=hive)
        rig.store.start_execution(SLUG, record, "")
        copy = {SLUG: {"t1": {"progress.md": f"- step on {hive}\n"}}}
        rig.store.redis.set(ledger_workspace.files_key(hive), json.dumps(copy))

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"][orders.SEEDS]["progress.md"] == "- step on hive-b\n"


@pytest.mark.feature("hub-admission-claim-context")
def test_a_plan_task_steering_carries_its_phase_evidence(rig: SimpleNamespace) -> None:
    from scripts.swarm_ledger import ledger_workspace

    planned = {**rig.rows["t1"], "kind": "plan"}

    seeds = rig.admitter._seeds(SLUG, planned, rig.found, "eng@sw-0009")  # pyright: ignore[reportPrivateUsage]

    assert seeds == {"steering.md": ledger_workspace.steering(planned, rig.found)}
    assert seeds["steering.md"] != ledger_workspace.steering(planned, None)


@pytest.mark.feature("hub-admission-claim-context")
def test_the_seeds_are_built_from_the_ledger_document_the_offer_was_read_against(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[Any] = []

    def seeds(self: admission.Admission, slug: str, task: dict[str, Any], doc: Any, name: str) -> dict[str, str]:
        read.append(doc)
        return {}

    monkeypatch.setattr(admission.Admission, "_seeds", seeds)

    rig.admitter.admit(SLUG, ask())

    assert read == [rig.found]
    assert read[0] is rig.found
