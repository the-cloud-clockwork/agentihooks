import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hive import account_slots
from scripts.hub import admission, slots
from scripts.swarm import admission_view, model_catalog
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.helpers import store_seed
from tests.helpers.narrow_ledger import DocLedger
from tests.swarm.test_readiness import doc, snapshot, task

SLUG = "sw"


@pytest.fixture
def rig():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 2, 2))
    found = doc([task("t1"), task("t2", lane="ci", pr_url="https://example.invalid/pull/1")])
    rows = {row["id"]: row for row in found["tasks"]}
    ledger: Any = DocLedger(found)
    source = SimpleNamespace(
        slugs=lambda: [SLUG],
        snapshot=lambda slug: snapshot(found, claimed=[t for t in rows if store.claimant(slug, t)]),
    )
    admitter = admission.Admission(store, ledger, source, clock=lambda: 7_000)
    admitter.view = admission_view.Narrow(lambda: store, ledger, cast(Any, source), claims=claimant_only)
    yield SimpleNamespace(store=store, ledger=ledger, rows=rows, admitter=admitter, found=found)
    store.redis.close()


def claimant_only(store: RedisStore, slug: str) -> Callable[[str], bool]:
    """The rig's claimed check: a task is claimed only while the store names its claimant."""
    return lambda task_id: store.claimant(slug, task_id) is not None


def ask(**fields):
    return {"hive": "hive-a", "task": "t1", "lane": "eng", "generation": 0, **fields}


def task_copy(hive: str) -> str:
    """The key under its own prefix where a hive keeps its copy of task t1's work folder."""
    from scripts.swarm_ledger import ledger_workspace

    return f"{ledger_workspace.files_key(hive)}:{SLUG}:t1"


@pytest.mark.feature("swarm-claim-lives")
@pytest.mark.parametrize("lives", [3, 4])
def test_events_admission_blocks_spent_claim_lives_before_creating_an_execution(rig, lives, capsys):
    from scripts.gates.claims import refusal

    rig.ledger.comments = []
    rig.ledger.comment = lambda *args, **kwargs: rig.ledger.comments.append((args, kwargs))
    for _ in range(lives):
        store_seed.count_claim(rig.store, SLUG, "t1")
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
        store_seed.count_claim(rig.store, SLUG, "t1")

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


@pytest.mark.feature("admission-launch-carries-admitted-execution")
def test_the_admitted_task_carries_its_launch_execution_identity(rig: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())

    [agent] = rig.store.agents(SLUG)
    spawned = admitted["task"]
    assert spawned["admitted_execution_id"] == agent.execution_id
    assert spawned["execution_id"] == agent.execution_id


@pytest.mark.feature("admission-launch-carries-admitted-execution")
def test_the_admitted_task_launch_arguments_pass_the_admitted_execution(rig: Any) -> None:
    from dataclasses import replace

    from scripts.swarm_v2.runtime import codex

    admitted = rig.admitter.admit(SLUG, ask())

    spawned = admitted["task"]
    config = replace(rig.store.config(SLUG), codex_inbox_transport="app_server")
    argv = codex.arguments(config, "codex", spawned)
    assert argv[:4] == ["--codex-transport", "app_server", "--seat", "eng-1@sw"]
    assert argv[argv.index("--execution") + 1] == spawned["admitted_execution_id"]


@pytest.mark.feature("codex-adopts-hub-admitted-execution")
def test_a_hub_admitted_codex_launch_adopts_the_admitted_execution(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.codex_session import supervisor
    from scripts.swarm import commands
    from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord
    from scripts.swarm_v2.runtime import codex

    rig.store.update(SLUG, codex_inbox_transport=APP_SERVER_TRANSPORT)
    admitted = rig.admitter.admit(SLUG, ask())
    argv = codex.arguments(rig.store.config(SLUG), "codex", admitted["task"])
    seat, execution = argv[argv.index("--seat") + 1], argv[argv.index("--execution") + 1]
    monkeypatch.setattr(commands, "hive_id", lambda: admitted["hive"])
    agent = AgentRecord(admitted["name"], "eng", "t1", seat=seat, model="gpt-5", effort="high")
    launch = supervisor.Launch(SLUG, agent, "/repo", "", {}, execution_id=execution)

    adopted = supervisor.admit(rig.store, launch, supervisor.Deps())

    assert (adopted.execution_id, adopted.model, adopted.effort) == (execution, "gpt-5", "high")
    [only] = rig.store.executions(SLUG, seat)
    assert (only.execution_id, only.state, only.harness, only.transport, only.hive) == (
        execution,
        "starting",
        "codex",
        APP_SERVER_TRANSPORT,
        "hive-a",
    )


LAUNCH = {"harness": "codex", "profile": "engineer", "account": "work", "model": "gpt-5", "effort": "high"}
HUB_CHECK = {"task": "t1", "at": 7_000, "relaunch": True, "owner": "hub"}


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


@pytest.mark.feature("events-launch-check")
def test_the_first_launch_decision_starts_a_launch_check_the_tick_retires_and_relaunches_on(
    rig: SimpleNamespace,
) -> None:
    from scripts.swarm import launch_check

    admitted = rig.admitter.admit(SLUG, ask())

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True

    assert launch_check.pending(rig.store, SLUG) == {admitted["name"]: HUB_CHECK}


@pytest.mark.feature("events-launch-check")
def test_a_republished_launch_decision_starts_no_second_launch_check(rig: SimpleNamespace) -> None:
    from scripts.swarm import launch_check

    admitted = rig.admitter.admit(SLUG, ask())
    rig.admitter.launched(SLUG, ask(launch=LAUNCH))
    launch_check.forget(rig.store, SLUG, admitted["name"])

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True

    assert launch_check.pending(rig.store, SLUG) == {}


@pytest.mark.feature("events-launch-check")
def test_a_republished_decision_naming_no_harness_starts_no_second_launch_check(rig: SimpleNamespace) -> None:
    from scripts.swarm import launch_check

    admitted = rig.admitter.admit(SLUG, ask())
    unnamed = {**LAUNCH, "harness": ""}
    rig.admitter.launched(SLUG, ask(launch=unnamed))
    assert launch_check.pending(rig.store, SLUG) == {admitted["name"]: HUB_CHECK}
    launch_check.forget(rig.store, SLUG, admitted["name"])

    assert rig.admitter.launched(SLUG, ask(launch=unnamed)) is True

    assert launch_check.pending(rig.store, SLUG) == {}


@pytest.mark.feature("events-launch-check")
def test_a_launch_check_the_hub_starts_misses_a_hive_agent_that_never_reported_its_join(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import commands, launch_check

    monkeypatch.setattr(commands, "hive_id", lambda: "root")

    admitted = rig.admitter.admit(SLUG, ask())
    rig.admitter.launched(SLUG, ask(launch=LAUNCH))

    [agent] = [agent for agent in rig.store.agents(SLUG) if agent.name in launch_check.pending(rig.store, SLUG)]
    found = launch_check.misses(rig.store, SLUG, agent, {}, {}, False, [])

    assert agent.name == admitted["name"]
    assert found == {"hive_join": {"expected": "session join report", "actual": "missing"}}
    assert not set(found) <= launch_check.REPORT_ONLY


@pytest.mark.feature("events-launch-check")
def test_a_refused_launch_decision_starts_no_launch_check(rig: SimpleNamespace) -> None:
    from scripts.swarm import launch_check

    admitted = rig.admitter.admit(SLUG, ask())
    rig.store.drop_agent(SLUG, admitted["name"])

    assert rig.admitter.launched(SLUG, ask(hive="hive-b", launch=LAUNCH)) is False
    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is False

    assert launch_check.pending(rig.store, SLUG) == {admitted["name"]: HUB_CHECK}


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


@pytest.mark.feature("hub-admission-relaunch-assignment")
def test_a_reported_launch_clears_the_saved_launch_assignment_so_the_next_life_takes_its_handoff_seat(
    rig: SimpleNamespace,
) -> None:
    rig.store.redis.hset(rig.store.key(SLUG, "launch-assignments"), "t1", json.dumps({"seat": "eng-3@sw"}))
    rig.admitter.admit(SLUG, ask())

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True

    assert rig.store.redis.hget(rig.store.key(SLUG, "launch-assignments"), "t1") is None


@pytest.mark.feature("hub-admission-relaunch-assignment")
def test_a_refused_launch_report_keeps_the_saved_launch_assignment(rig: SimpleNamespace) -> None:
    saved = json.dumps({"seat": "eng-3@sw"})
    rig.store.redis.hset(rig.store.key(SLUG, "launch-assignments"), "t1", saved)
    rig.admitter.admit(SLUG, ask())

    assert rig.admitter.launched(SLUG, ask(hive="hive-b", launch=LAUNCH)) is False

    assert rig.store.redis.hget(rig.store.key(SLUG, "launch-assignments"), "t1") == saved


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
def test_a_late_events_admission_keeps_its_transfer_with_the_admitted_successor(rig: SimpleNamespace) -> None:
    from scripts.handoff import transfers

    transfer = left_transfer(rig)
    rig.admitter.clock = late_clock(5)

    name = rig.admitter.admit(SLUG, ask())["name"]

    assert transfers.get(rig.store, SLUG, transfer["id"])["successor"] == name
    assert rig.store.claimant(SLUG, "t1") == name


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_catalog, "catalog", Mock(return_value={"claude": [{"model": "sonnet-lane"}], "codex": []})
    )
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


@pytest.mark.feature("hub-admission-unlisted-retry")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("missing", None),
        ("name", "another-agent"),
        ("execution_id", "another-execution"),
        ("generation", 99),
        ("state", "done"),
        ("launched_at", 1_500),
        ("claim", None),
    ],
)
def test_an_unlisted_kept_admission_requires_its_current_unlaunched_claimant(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    from dataclasses import replace

    first = rig.admitter.admit(SLUG, ask())
    assert first is not None
    name = first["name"]
    task = first["task"]
    agent = rig.store.execution_occupants(SLUG)[task["seat"]]
    rig.store.redis.hdel(rig.store.key(SLUG, "agents"), name)
    if field == "claim":
        rig.store.release(SLUG, "t1", name)
    else:
        occupants = {} if field == "missing" else {task["seat"]: replace(agent, **{field: value})}

        def current(slug: str) -> dict[str, Any]:
            assert slug == SLUG
            return occupants

        monkeypatch.setattr(rig.store, "execution_occupants", current)

    assert rig.admitter.admit(SLUG, ask()) is None
    assert len(rig.ledger.writes) == 1


@pytest.mark.feature("hub-admission-unlisted-retry")
def test_another_hive_never_receives_an_unlisted_starting_admission(rig: SimpleNamespace) -> None:
    first = rig.admitter.admit(SLUG, ask())
    assert first is not None
    rig.store.redis.hdel(rig.store.key(SLUG, "agents"), first["name"])

    assert rig.admitter.admit(SLUG, ask(hive="hive-b")) is None
    assert rig.store.claimant(SLUG, "t1") == first["name"]
    assert rig.admitter.admit(SLUG, ask()) == first
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


@pytest.mark.feature("dead-offer-generation")
def test_a_dead_lettered_reopen_with_no_admission_held_advances_the_claim_generation(rig: SimpleNamespace) -> None:
    rig.rows["t1"]["claim_generation"] = 4

    assert rig.admitter.reopen(SLUG, ask(generation=4, dead=True)) is False

    row = rig.rows["t1"]
    assert (row["state"], row.get("claimed_by", ""), row["claim_generation"]) == ("open", "", 5)
    assert rig.ledger.writes == [
        (SLUG, "t1", {"claim_generation": 5}, ("open",), {"claimed_by": "", "claim_generation": 4})
    ]
    assert rig.admitter.reopen(SLUG, ask(generation=3, dead=True)) is False
    assert row["claim_generation"] == 5
    assert rig.admitter.reopen(SLUG, ask(generation=5, dead=True)) is False
    assert row["claim_generation"] == 6
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []


def test_a_reopen_with_no_admission_held_advances_nothing_unless_its_offer_dead_lettered(rig: SimpleNamespace) -> None:
    assert rig.admitter.reopen(SLUG, ask()) is False
    assert rig.admitter.reopen(SLUG, ask(dead=False)) is False

    assert rig.ledger.writes == []
    assert rig.rows["t1"].get("claim_generation", 0) == 0


def test_a_dead_lettered_reopen_of_a_claimed_task_advances_nothing(rig: SimpleNamespace) -> None:
    rig.rows["t1"].update(state="claimed", claimed_by="engineer-1", claim_generation=2)

    assert rig.admitter.reopen(SLUG, ask(generation=1, dead=True)) is False

    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claim_generation"]) == ("claimed", 2)


def test_a_dead_lettered_reopen_leaves_the_admission_another_hive_holds(rig: SimpleNamespace) -> None:
    name = rig.admitter.admit(SLUG, ask(hive="hive-b"))["name"]
    writes = len(rig.ledger.writes)

    assert rig.admitter.reopen(SLUG, ask(dead=True)) is False

    assert rig.ledger.writes[writes:] == []
    assert rig.store.claimant(SLUG, "t1") == name


def test_a_dead_lettered_reopen_of_its_own_admission_releases_it(rig: SimpleNamespace) -> None:
    name = rig.admitter.admit(SLUG, ask())["name"]

    assert rig.admitter.reopen(SLUG, ask(dead=True)) is True

    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.ledger.writes[-1] == (
        SLUG,
        "t1",
        {"state": "open", "claimed_by": ""},
        ("claimed", "pr"),
        {"claimed_by": name},
    )


@pytest.mark.parametrize(
    "failure",
    ["LedgerRefused", "LedgerUnreachable"],
)
def test_a_failed_generation_advance_is_logged_and_answered_false(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    from scripts.swarm import ledger_client

    def refuse(*args: Any, **kwargs: Any) -> dict:
        raise getattr(ledger_client, failure)("ledger sw refused: task_update may set only")

    rig.ledger.update_task = refuse

    assert rig.admitter.reopen(SLUG, ask(generation=2, dead=True)) is False
    assert capsys.readouterr().err.splitlines() == [
        "hub: events reopen swarm=sw task=t1 generation=2: claim generation not advanced, "
        "ledger sw refused: task_update may set only"
    ]


@pytest.mark.feature("hub-admission-late-answer-keeps-claim")
def test_an_admission_finished_after_the_hive_stopped_waiting_keeps_its_claim_for_the_hive(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.admitter.clock = late_clock(0)

    admitted = rig.admitter.admit(SLUG, ask())

    assert f"late: finished after {admission.ANSWER_MS} ms" in capsys.readouterr().err

    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert [agent.name for agent in rig.store.agents(SLUG)] == [admitted["name"]]
    row = rig.rows["t1"]
    assert (row["state"], row["claimed_by"], row["claim_generation"]) == ("claimed", admitted["name"], 1)
    assert rig.admitter.admit(SLUG, ask()) == admitted
    assert len(rig.ledger.writes) == 1


@pytest.mark.feature("hub-admission-late-answer-keeps-claim")
def test_an_admission_whose_ledger_write_takes_eight_seconds_still_yields_a_live_claimant(
    rig: SimpleNamespace,
) -> None:
    now = [7_000]
    rig.admitter.clock = lambda: now[0]
    write = rig.ledger.update_task

    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        now[0] += 8_000
        written: dict[str, Any] = write(*args, **kwargs)
        return written

    rig.ledger.update_task = slow

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["name"] in rig.store.listed(SLUG)
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert rig.rows["t1"]["claimed_by"] == admitted["name"]


@pytest.mark.feature("hub-admission-late-answer-keeps-claim")
def test_an_admission_finished_just_inside_the_hive_wait_is_kept(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.admitter.clock = late_clock(-1)

    name = rig.admitter.admit(SLUG, ask())["name"]

    assert rig.store.claimant(SLUG, "t1") == name
    assert rig.rows["t1"]["state"] == "claimed"
    assert "late:" not in capsys.readouterr().err


@pytest.mark.feature("hub-admission-hive-asks-again")
def test_a_repeat_ask_while_the_hives_own_admission_holds_its_turn_is_held_and_another_hive_gets_none(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import admission as hive_admission

    monkeypatch.setattr(admission, "TURN_TRIES", 1)
    monkeypatch.setattr(admission, "TURN_WAIT_S", 0)
    other = admission.Admission(rig.store, rig.ledger, rig.admitter.source, clock=lambda: 7_000)
    write = rig.admitter._mark
    asked: list[dict[str, Any] | None] = []

    def slow(*args: Any) -> dict[str, Any] | None:
        asked.extend([other.admit(SLUG, ask()), other.admit(SLUG, ask(hive="hive-b"))])
        written: dict[str, Any] | None = write(*args)
        return written

    monkeypatch.setattr(rig.admitter, "_mark", slow)

    admitted = rig.admitter.admit(SLUG, ask())

    assert asked == [{"held": hive_admission.ADMITTING}, None]
    assert other.admit(SLUG, ask()) == admitted
    assert [agent.name for agent in rig.store.agents(SLUG)] == [admitted["name"]]
    assert len(rig.ledger.writes) == 1


@pytest.mark.feature("hub-admission-hive-asks-again")
@pytest.mark.parametrize("holder", ["", "another-offer token"], ids=["lapsed", "taken"])
def test_a_repeat_ask_while_the_hives_own_admission_still_claims_past_its_turn_key_is_held(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, holder: str
) -> None:
    from scripts.hive import admission as hive_admission
    from scripts.swarm import tick

    turn, offer = admission.turn_key("hive-a"), rig.admitter._key(SLUG, ask())
    mark = f"{offer}:claiming"
    other = admission.Admission(rig.store, rig.ledger, rig.admitter.source, clock=lambda: 7_000)
    write = rig.admitter._mark
    asked: list[dict[str, Any] | None] = []
    marked: list[Any] = []

    def lapsed(*args: Any) -> dict[str, Any] | None:
        rig.store.redis.delete(turn)
        if holder:
            rig.store.redis.set(turn, holder)
        asked.extend([other.admit(SLUG, ask()), other.admit(SLUG, ask(hive="hive-b"))])
        marked.extend([rig.store.redis.get(mark), rig.store.redis.pttl(mark)])
        written: dict[str, Any] | None = write(*args)
        return written

    monkeypatch.setattr(rig.admitter, "_mark", lapsed)

    admitted = rig.admitter.admit(SLUG, ask())

    assert asked == [{"held": hive_admission.ADMITTING}, None]
    assert marked[0] == "hive-a"
    assert tick.LEASE_MS - 5_000 < marked[1] <= tick.LEASE_MS
    assert rig.store.redis.exists(mark) == 0
    assert other.admit(SLUG, ask()) == admitted
    assert [agent.name for agent in rig.store.agents(SLUG)] == [admitted["name"]]
    assert len(rig.ledger.writes) == 1


@pytest.mark.feature("hub-admission-hive-asks-again")
@pytest.mark.parametrize("ending", ["refused", "raised"])
def test_an_admission_claim_that_ends_without_an_admission_leaves_no_claiming_mark(
    rig: SimpleNamespace, ending: str
) -> None:
    mark = f"{rig.admitter._key(SLUG, ask())}:claiming"
    marked: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        marked.append(rig.store.redis.get(mark))
        if ending == "raised":
            raise ConnectionError("ledger down")
        return {**rig.rows["t1"], "state": "claimed", "claimed_by": "other"}

    rig.ledger.update_task = refuse

    if ending == "raised":
        with pytest.raises(ConnectionError, match="^ledger down$"):
            rig.admitter.admit(SLUG, ask())
    else:
        assert rig.admitter.admit(SLUG, ask()) is None

    assert marked == ["hive-a"]
    assert rig.store.redis.exists(mark) == 0
    assert rig.store.claimant(SLUG, "t1") is None


@pytest.mark.feature("hub-admission-hive-asks-again")
def test_an_admission_claim_leaves_a_claiming_mark_another_hive_wrote_in_place(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    mark = f"{rig.admitter._key(SLUG, ask())}:claiming"
    write = rig.admitter._mark

    def overwritten(*args: Any) -> dict[str, Any] | None:
        rig.store.redis.set(mark, "hive-b")
        written: dict[str, Any] | None = write(*args)
        return written

    monkeypatch.setattr(rig.admitter, "_mark", overwritten)

    assert rig.admitter.admit(SLUG, ask()) is not None
    assert rig.store.redis.get(mark) == "hive-b"


@pytest.mark.feature("hub-admission-hive-asks-again")
def test_a_repeat_ask_whose_own_ask_was_kept_before_its_turn_answers_that_admission(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = admission.Admission(rig.store, rig.ledger, rig.admitter.source, clock=lambda: 7_000)
    placement = other._unplaceable
    kept: list[dict[str, Any] | None] = []

    def admitting(row: dict[str, Any], hive: str) -> str:
        kept.append(rig.admitter.admit(SLUG, ask()))
        unplaceable: str = placement(row, hive)
        return unplaceable

    monkeypatch.setattr(other, "_unplaceable", admitting)

    again = other.admit(SLUG, ask())

    assert again is not None
    assert again == kept[0]
    assert [agent.name for agent in rig.store.agents(SLUG)] == [again["name"]]
    assert len(rig.ledger.writes) == 1
    assert other.admit(SLUG, ask(hive="hive-b")) is None


@pytest.mark.feature("hub-admission-hive-asks-again")
def test_a_repeat_ask_waiting_on_the_hives_turn_answers_its_own_admission_once_it_leaves_the_turn(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn, offer = admission.turn_key("hive-a"), rig.admitter._key(SLUG, ask())
    write = rig.admitter._mark
    holders: list[str] = []

    def marking(*args: Any) -> dict[str, Any] | None:
        holders.append(rig.store.redis.get(turn))
        written: dict[str, Any] | None = write(*args)
        return written

    monkeypatch.setattr(rig.admitter, "_mark", marking)
    admitted = rig.admitter.admit(SLUG, ask())
    kept = rig.store.redis.get(offer)
    rig.store.redis.delete(offer)
    rig.store.redis.set(turn, holders[0])

    def leave(seconds: float) -> None:
        rig.store.redis.set(offer, kept)
        rig.store.redis.delete(turn)

    monkeypatch.setattr(admission, "time", SimpleNamespace(sleep=leave))
    other = admission.Admission(rig.store, rig.ledger, rig.admitter.source, clock=lambda: 7_000)

    assert other.admit(SLUG, ask()) == admitted
    assert other.admit(SLUG, ask(hive="hive-b")) is None
    assert rig.store.redis.get(turn) is None
    assert len(rig.ledger.writes) == 1


@pytest.mark.feature("hub-admission-hive-asks-again")
@pytest.mark.parametrize("listed", [True, False])
def test_a_repeat_ask_that_read_the_offer_claimed_by_its_own_finished_admission_answers_that_admission(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, listed: bool
) -> None:
    other = admission.Admission(rig.store, rig.ledger, rig.admitter.source, clock=lambda: 7_000)
    kept: list[dict[str, Any] | None] = []

    read = other.view.task

    def task(slug: str, task_id: str, mark: Callable[[str], None]) -> Any:
        kept.append(rig.admitter.admit(SLUG, ask()))
        if not listed:
            rig.store.redis.hdel(rig.store.key(SLUG, "agents"), rig.store.claimant(SLUG, "t1"))
        return read(slug, task_id, mark)

    monkeypatch.setattr(other.view, "task", task)

    again = other.admit(SLUG, ask())

    assert again is not None
    assert again == kept[0]
    assert len(rig.ledger.writes) == 1


@pytest.mark.feature("hub-admission-hive-asks-again")
@pytest.mark.parametrize("listed", [True, False])
def test_a_late_kept_admission_launches_on_the_hives_next_ask_after_readiness_withdrew_its_offer(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], listed: bool
) -> None:
    from unittest.mock import Mock

    from scripts import serena_router_daemon
    from scripts.events.config import QueuePolicy
    from scripts.hive import lanes
    from scripts.hive.spoke import Spoke, SpokeConfig
    from scripts.swarm import readiness
    from scripts.swarm_v2.runtime.base import Outcome, Status

    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    bus: Any = open_bus(BusConfig())
    slots.serve(bus, admitter=rig.admitter)
    request = type(bus).request
    asks: list[str] = []

    def late_first(self: Any, subject: str, data: bytes, timeout_ms: int) -> bytes:
        answer: bytes = request(self, subject, data, timeout_ms)
        asks.append(json.loads(data)["op"])
        if asks == ["admit"]:
            raise TimeoutError(f"no responders for {subject!r}")
        return answer

    monkeypatch.setattr(type(bus), "request", late_first)
    rig.admitter.clock = late_clock(0)
    runtime = Mock()
    placed = SimpleNamespace(pane_id="pane-a", harness="claude", account="slot-a", launched_at=1_500)
    runtime.spawn.return_value = Outcome("spawn", Status.OK, "local", placed)
    reading = {"cpus": 32, "load5": 0, "available_mb": 60_000, "total_mb": 64_000}
    tasks = lanes.LaneTasks(bus, "hive-a", rig.store.config)
    config = SpokeConfig(rig.store.config(SLUG), "eng", "hive-a")
    spoke = Spoke(config, bus, runtime, tasks, lambda: reading, view=Mock(return_value=None))
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    offered: list[str] = []

    def unsettled(event: Any) -> bool:
        offered.append(event.key)
        return False

    try:
        readiness.publish(bus, rig.admitter.source.snapshot(SLUG), 1)
        assert spoke.poll() == "asked"
        name = rig.store.claimant(SLUG, "t1")
        if not listed:
            rig.store.redis.hdel(rig.store.key(SLUG, "agents"), name)
        queue.withdraw(unsettled)
        readiness.publish(bus, rig.admitter.source.snapshot(SLUG), 2)
        queue.withdraw(unsettled)
        assert spoke.poll() == "started"
    finally:
        bus.close()

    assert offered == ["t1"]
    assert "late: finished after" in capsys.readouterr().err
    [launched] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert launched.name == name
    assert rig.store.claimant(SLUG, "t1") == name
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claim_generation"]) == ("claimed", 1)
    assert asks == ["admit", "admit", "launched"]


@pytest.mark.feature("hub-late-admission-retry-launches")
def test_a_late_kept_admission_launches_on_the_hives_next_ask_after_a_reap_judged_a_row_read_before_its_claim(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import copy

    from scripts import serena_router_daemon
    from scripts.events.config import QueuePolicy
    from scripts.hive import lanes
    from scripts.hive.spoke import Spoke, SpokeConfig
    from scripts.swarm import readiness, tick
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm_v2.runtime.base import Outcome, Status
    from scripts.tick.steps import agents

    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    monkeypatch.setenv("SWARM_HIVE_ID", "workstation")
    bus: Any = open_bus(BusConfig())
    slots.serve(bus, admitter=rig.admitter)
    request = type(bus).request
    asks: list[str] = []

    def late_first(self: Any, subject: str, data: bytes, timeout_ms: int) -> bytes:
        answer: bytes = request(self, subject, data, timeout_ms)
        asks.append(json.loads(data)["op"])
        if asks == ["admit"]:
            raise TimeoutError(f"no responders for {subject!r}")
        return answer

    monkeypatch.setattr(type(bus), "request", late_first)
    rig.admitter.clock = late_clock(0)
    runtime = Mock()
    placed = SimpleNamespace(pane_id="pane-a", harness="claude", account="slot-a", launched_at=1_500)
    runtime.spawn.return_value = Outcome("spawn", Status.OK, "local", placed)
    reading = {"cpus": 32, "load5": 0, "available_mb": 60_000, "total_mb": 64_000}
    tasks = lanes.LaneTasks(bus, "hive-a", rig.store.config)
    config = SpokeConfig(rig.store.config(SLUG), "eng", "hive-a")
    spoke = Spoke(config, bus, runtime, tasks, lambda: reading, view=Mock(return_value=None))
    queue = bus.queue(readiness.queue_name(SLUG, "eng"), QueuePolicy())
    pass_rows = copy.deepcopy(rig.rows)
    gone = SimpleNamespace(
        live_names=set,
        observe=lambda agent: PaneObservation("unknown"),
        retire=lambda agent, homes=(): True,
        refusal=lambda agent: {},
        reported=lambda agent: False,
    )

    def unsettled(event: Any) -> bool:
        return False

    try:
        readiness.publish(bus, rig.admitter.source.snapshot(SLUG), 1)
        assert spoke.poll() == "asked"
        name = rig.store.claimant(SLUG, "t1")
        [admitted] = [agent for agent in rig.store.agents(SLUG) if agent.name == name]
        agents._reap_agent(tick._Reaping(SLUG, rig.store, SimpleNamespace(), gone, pass_rows, set(), 7_000), admitted)
        queue.withdraw(unsettled)
        readiness.publish(bus, rig.admitter.source.snapshot(SLUG), 2)
        queue.withdraw(unsettled)
        assert spoke.poll() == "started"
    finally:
        bus.close()

    assert "late: finished after" in capsys.readouterr().err
    [launched] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert launched.name == name
    assert rig.store.claimant(SLUG, "t1") == name
    assert asks == ["admit", "admit", "launched"]


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

    rig.admitter.admit(SLUG, ask())

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a late: finished after {admission.ANSWER_MS + 5} ms, "
        f"past the {admission.ANSWER_MS} ms answer budget, claim kept release={admission.ANSWER_MS + 5} held=0 stopped=0 "
        f"row=0 rows=0 parts=0 withdrawal=0 placement=0 cap=0 turn=0 full=0 claim=0 seat=0 execution=0 ledger=0 "
        f"continuation=0 seeds=0 primed=0 transfer=0"
    ]


@pytest.mark.feature("hub-slot-serve-drops-abandoned-requests")
def test_a_late_admission_counts_the_time_its_request_waited_in_the_hub_serve_queue(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.events.bus import SERVE_WAITED_MS

    rig.admitter.clock = lambda: 7_000
    waited = SERVE_WAITED_MS.set(admission.ANSWER_MS)
    try:
        rig.admitter.admit(SLUG, ask())
    finally:
        SERVE_WAITED_MS.reset(waited)

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a late: finished after {admission.ANSWER_MS} ms, "
        f"past the {admission.ANSWER_MS} ms answer budget, claim kept release=0 held=0 stopped=0 "
        f"row=0 rows=0 parts=0 withdrawal=0 placement=0 cap=0 turn=0 full=0 claim=0 seat=0 execution=0 ledger=0 "
        f"continuation=0 seeds=0 primed=0 transfer=0"
    ]


@pytest.mark.feature("hub-admission-step-timing")
def test_a_late_admission_line_names_the_elapsed_milliseconds_of_each_step(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import tick as tick_module

    now = [1000]
    rig.admitter.clock = lambda: now[0]

    def bump(amount: int, original: Callable[..., Any]) -> Callable[..., Any]:
        fired: list[bool] = []

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not fired:
                fired.append(True)
                now[0] += amount
            return original(*args, **kwargs)

        return wrapped

    original_turn = rig.admitter._turn

    @contextmanager
    def turn(hive: str, offer: str) -> Iterator[str]:
        now[0] += 800
        with original_turn(hive, offer) as busy:
            yield busy

    monkeypatch.setattr(rig.admitter, "_outdated", bump(100, rig.admitter._outdated))
    monkeypatch.setattr(rig.admitter, "_key", bump(200, rig.admitter._key))
    monkeypatch.setattr(admission.run_window, "claims_stopped", bump(300, admission.run_window.claims_stopped))
    monkeypatch.setattr(rig.ledger, "task", bump(400, rig.ledger.task))
    monkeypatch.setattr(admission.readiness, "withdrawal", bump(500, admission.readiness.withdrawal))
    monkeypatch.setattr(rig.admitter, "_unplaceable", bump(600, rig.admitter._unplaceable))
    monkeypatch.setattr(tick_module, "_lives_spent", bump(700, tick_module._lives_spent))
    monkeypatch.setattr(rig.admitter, "_turn", turn)
    monkeypatch.setattr(rig.admitter, "_full", bump(900, rig.admitter._full))
    monkeypatch.setattr(rig.store, "claim", bump(1000, rig.store.claim))
    monkeypatch.setattr(tick_module, "_free_seat", bump(1100, tick_module._free_seat))
    monkeypatch.setattr(rig.store, "start_execution", bump(1200, rig.store.start_execution))
    monkeypatch.setattr(rig.ledger, "update_task", bump(1300, rig.ledger.update_task))

    rig.admitter.admit(SLUG, ask())

    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a late: finished after 9100 ms, "
        f"past the {admission.ANSWER_MS} ms answer budget, claim kept release=100 held=200 stopped=300 row=400 rows=0 "
        f"parts=0 withdrawal=500 placement=600 cap=700 turn=800 full=900 claim=1000 seat=1100 execution=1200 "
        f"ledger=1300 continuation=0 seeds=0 primed=0 transfer=0"
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
def test_the_hub_admits_no_launch_for_a_hive_draining_for_a_release(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import registry

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    registry.beat(rig.store.redis, "hive-a", {"heartbeat_at": 6_000})
    registry.update(rig.store.redis, "hive-a", ["draining=yes"])

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted == {"held": "the hive is draining"}
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []
    assert rig.rows["t1"]["state"] == "open"
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events placement swarm={SLUG} task=t1 hive=hive-a: refused, the hive is draining"
    ]


@pytest.mark.feature("hub-admission-hive-local-held")
@pytest.mark.parametrize(
    "member, draining, fields",
    [
        ("2.17.843", "yes", {}),
        ("2.17.825", "no", {}),
        ("2.17.843", "no", {"workstation": True}),
        ("2.17.843", "no", {"hive": "hive-b"}),
        ("2.17.843", "no", {"needs": ["gpu"]}),
    ],
)
def test_a_hive_local_refusal_keeps_the_only_offer_for_an_eligible_second_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, member: str, draining: str, fields: dict[str, Any]
) -> None:
    from scripts import serena_router_daemon
    from scripts.hive import lanes, registry
    from scripts.hive.spoke import Spoke, SpokeConfig
    from scripts.swarm import readiness
    from scripts.swarm_v2.runtime.base import Outcome, Status

    _released(rig, monkeypatch, member, "2.17.843")
    registry.update(rig.store.redis, "hive-a", [f"draining={draining}"])
    destination = readiness.ROOT if fields.get("workstation") else "hive-b"
    registry.beat(rig.store.redis, destination, {"heartbeat_at": 6_000, "capabilities": ["gpu"]})
    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
    clock = SimpleNamespace(now=1_000)
    bus: Any = open_bus(BusConfig(), clock=lambda: clock.now)
    slots.serve(bus, admitter=rig.admitter)
    runtime, other_runtime = Mock(), Mock()
    other_runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        SimpleNamespace(pane_id="pane-b", harness="claude", account="slot-b", launched_at=1_500),
    )
    reading = {"cpus": 32, "load5": 0, "available_mb": 60_000, "total_mb": 64_000}
    config = SpokeConfig(rig.store.config(SLUG), "eng", "hive-a")
    spoke = Spoke(
        config,
        bus,
        runtime,
        lanes.LaneTasks(bus, "hive-a", rig.store.config),
        lambda: reading,
        view=Mock(return_value=None),
    )
    other = Spoke(
        SpokeConfig(config.swarm, "eng", destination),
        bus,
        other_runtime,
        lanes.LaneTasks(bus, destination, rig.store.config),
        lambda: reading,
        view=Mock(return_value=None),
    )
    try:
        readiness.publish(bus, rig.admitter.source.snapshot(SLUG), 1)
        rig.rows["t1"].update(fields)
        assert spoke.poll() == "held"
        runtime.spawn.assert_not_called()
        assert rig.store.claimant(SLUG, "t1") is None
        assert rig.ledger.writes == []
        clock.now += config.policy.ack_wait_ms
        assert other.poll() == "started"
        other_runtime.spawn.assert_called_once()
        launched = other_runtime.spawn.call_args.args[0]
        assert launched.task["id"] == "t1"
        assert rig.store.claimant(SLUG, "t1") == launched.name
        assert rig.rows["t1"]["state"] == "claimed"
    finally:
        bus.close()


@pytest.mark.feature("hub-admission-hive-local-held")
@pytest.mark.parametrize("fields", [{"state": "done"}, {"claim_generation": 1}, {"lane": "ci"}])
def test_a_refusal_about_the_task_stays_terminal(rig: SimpleNamespace, fields: dict[str, Any]) -> None:
    rig.rows["t1"].update(fields)

    assert rig.admitter.admit(SLUG, ask()) is None
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []


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


def _lineage_running(rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, slug: str, expert: str) -> str:
    """Hive A one below its cap of two beside a running expert lineage of the swarm whose expert, seated on another
    hive, is in the given state; no expert record at all when the state is empty. Returns the lineage id."""
    from dataclasses import replace

    from scripts.swarm import expert_lineage
    from scripts.swarm.store import AgentRecord
    from tests.swarm.test_expert_mission import GOOD

    _hive_holding(rig, monkeypatch, "2", 1)
    lineage = expert_lineage.start(rig.store, slug, GOOD, "/missions/batch.md", "xhigh", "master@a1b2c3-0001", 1_000)
    if expert:
        name = rig.store.next_name(slug, "expert")
        record = AgentRecord(name, "expert", lineage.id, seat=lineage.seat, started_at=1, hive="hive-b")
        agent = rig.store.start_execution(slug, record)
        rig.store.put_agent(slug, replace(agent, state=expert))
    return lineage.id


WAITING = "1 of 2 agents on the hive with 1 kept for expert lineages waiting for an expert"


@pytest.mark.feature("hub-admission-expert-room-reserve")
@pytest.mark.parametrize("expert", ["ended", "finished", ""])
def test_the_hub_holds_a_launch_on_a_hive_one_below_max_agents_while_a_running_expert_lineage_waits(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], expert: str
) -> None:
    _lineage_running(rig, monkeypatch, SLUG, expert)
    before = {slug: len(rig.store.agents(slug)) for slug in (SLUG, "other")}

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted == {"held": WAITING}
    assert rig.store.claimant(SLUG, "t1") is None
    assert {slug: len(rig.store.agents(slug)) for slug in (SLUG, "other")} == before
    assert rig.ledger.writes == []
    assert rig.rows["t1"]["state"] == "open"
    assert capsys.readouterr().err.splitlines() == [
        f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: {WAITING}"
    ]


@pytest.mark.feature("hub-admission-expert-room-reserve")
def test_a_lineage_of_another_swarm_waiting_for_an_expert_also_holds_the_hive(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lineage_running(rig, monkeypatch, "other", "")

    assert rig.admitter.admit(SLUG, ask()) == {"held": WAITING}
    assert rig.store.claimant(SLUG, "t1") is None


@pytest.mark.feature("hub-admission-expert-room-reserve")
@pytest.mark.parametrize("expert, stopped", [("working", False), ("starting", False), ("", True)])
def test_the_hub_admits_a_launch_below_max_agents_while_no_running_expert_lineage_waits(
    rig: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    expert: str,
    stopped: bool,
) -> None:
    from scripts.swarm import expert_lineage

    lineage = _lineage_running(rig, monkeypatch, SLUG, expert)
    if stopped:
        expert_lineage.stop(rig.store, SLUG, lineage, "master@a1b2c3-0001", "mission dropped", 2_000)

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert capsys.readouterr().err == ""


@pytest.mark.feature("hub-admission-expert-room-reserve")
def test_the_slot_kept_for_a_waiting_lineage_goes_to_its_successor_and_then_the_hive_is_full(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import AgentRecord

    lineage = _lineage_running(rig, monkeypatch, SLUG, "")

    assert rig.admitter.admit(SLUG, ask()) == {"held": WAITING}

    seat = f"{lineage}@{SLUG}"
    successor = AgentRecord(
        rig.store.next_name(SLUG, "expert"), "expert", lineage, seat=seat, started_at=2, hive="hive-a"
    )
    rig.store.start_execution(SLUG, successor)

    assert rig.admitter.admit(SLUG, ask()) == {"held": "2 of 2 agents on the hive"}
    assert rig.store.claimant(SLUG, "t1") is None


@pytest.mark.feature("hub-admission-expert-room-reserve")
def test_a_hive_that_takes_no_expert_keeps_no_slot_for_a_waiting_lineage(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import registry

    _lineage_running(rig, monkeypatch, SLUG, "")
    registry.update(rig.store.redis, "hive-a", ["excluded=master,expert"])

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]


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

    assert admitted == {"held": f"release {member} below hub release {hub}"}
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

    assert admitted == {"held": "release 2.17.825 below hub release 2.17.843"}
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


@pytest.mark.feature("hub-admission-pinned-hive")
def test_the_hub_admits_a_launch_for_a_pinned_hive_on_an_older_release(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import registry

    _released(rig, monkeypatch, "2.17.825", "2.17.843")
    registry.beat(rig.store.redis, "hive-a", {"pinned": "yes"})

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert admitted["hive"] == "hive-a"
    assert capsys.readouterr().err == ""


@pytest.mark.feature("hub-admission-pinned-hive")
@pytest.mark.parametrize("pinned", ["no", "", "true"])
def test_the_hub_refuses_a_hive_not_marked_pinned_yes_on_an_older_release(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pinned: str
) -> None:
    from scripts.hive import registry

    _released(rig, monkeypatch, "2.17.825", "2.17.843")
    registry.beat(rig.store.redis, "hive-a", {"pinned": pinned})

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted == {"held": "release 2.17.825 below hub release 2.17.843"}
    assert rig.store.claimant(SLUG, "t1") is None


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

    named = rig.store.next_name(SLUG, "eng", 1)
    earlier = AgentRecord(named, "eng", "t1", seat="eng-9@sw", state="starting", started_at=1, hive="hive-b")
    rig.store.start_execution(SLUG, earlier, "")
    synced = {"progress.md": "- earlier step\n", "steering.md": "stale"}
    rig.store.redis.set(task_copy("hive-b"), json.dumps(synced))

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


@pytest.mark.feature("hub-admission-earlier-lives-launched-only")
def test_the_branch_heads_query_asks_only_for_earlier_lives_that_launched(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.store.update(SLUG, repo_origin="github.com/acme/app")
    for name, state, pane, ended in (
        ("eng@sw-0001", "finished", "w1:p1", 1),
        ("eng@sw-0002", "starting", "", 2),
        ("eng@sw-0003", "starting", "", 3),
    ):
        life = {"task": "t1", "lane": "eng", "name": name, "state": state, "pane_id": pane, "ended_at": ended}
        rig.store.redis.rpush(rig.store.key(SLUG, "history"), json.dumps(life))
    asked = _github_heads(monkeypatch, {"b0": {"target": {"oid": "abc123"}}})

    admitted = rig.admitter.admit(SLUG, ask())

    assert asked == [{"owner": "acme", "name": "app", "b0": "refs/heads/eng-sw-0001"}]
    assert admitted["task"]["reclaim"]["continue_from"] == "origin/eng-sw-0001"


@pytest.mark.feature("hub-admission-earlier-lives-launched-only")
def test_a_life_the_hub_freed_before_any_hive_started_it_reads_no_branch_heads_on_the_next_admission(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hub import branch_heads

    first = rig.admitter.admit(SLUG, ask())
    [agent] = rig.store.agents(SLUG)
    rig.admitter.free(SLUG, agent)

    def unread(config: SwarmConfig) -> None:
        raise AssertionError("a life no hive started pushed no branch")

    monkeypatch.setattr(branch_heads, "reclaimer", unread)

    admitted = rig.admitter.admit(SLUG, ask(generation=2))

    [life] = [json.loads(raw) for raw in rig.store.redis.lrange(rig.store.key(SLUG, "history"), 0, -1)]
    assert (life["name"], life["state"], life["pane_id"]) == (first["name"], "starting", "")
    assert "reclaim" not in admitted["task"]
    assert rig.store.reclaims(SLUG) == {}


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

    for started, hive in ((1, "hive-c"), (5, "hive-b")):
        named = rig.store.next_name(SLUG, "eng", started)
        record = AgentRecord(named, "eng", "t1", seat=f"eng-{started + 7}@sw", started_at=started, hive=hive)
        rig.store.start_execution(SLUG, record, "")
        copy = {"progress.md": f"- step on {hive}\n"}
        rig.store.redis.set(task_copy(hive), json.dumps(copy))

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"][orders.SEEDS]["progress.md"] == "- step on hive-b\n"


@pytest.mark.feature("work-folder-follows-the-task-across-hives")
def test_a_launched_report_records_the_hive_holding_the_task_work_folder(rig: SimpleNamespace) -> None:
    key = rig.store.key(SLUG, "work-folder", "t1")
    rig.admitter.admit(SLUG, ask())
    assert rig.store.redis.get(key) is None

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True

    assert rig.store.redis.get(key) == "hive-a"
    assert rig.store.redis.ttl(key) == -1


@pytest.mark.feature("work-folder-follows-the-task-across-hives")
def test_the_seeds_follow_the_hive_that_last_launched_the_task_after_its_agent_retired(rig: SimpleNamespace) -> None:
    from scripts.hive import orders

    copy = {"progress.md": "- step on hive-b\n", "proof.md": "- proof on hive-b\n"}
    rig.store.redis.set(rig.store.key(SLUG, "work-folder", "t1"), "hive-b")
    rig.store.redis.set(task_copy("hive-b"), json.dumps(copy))

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"][orders.SEEDS] == {**copy, "steering.md": "# t1: title t1\n"}


@pytest.mark.feature("work-folder-follows-the-task-across-hives")
def test_the_hive_that_last_launched_the_task_wins_over_an_earlier_agents_hive(rig: SimpleNamespace) -> None:
    from scripts.hive import orders
    from scripts.swarm.store import AgentRecord

    named = rig.store.next_name(SLUG, "eng", 1)
    earlier = AgentRecord(named, "eng", "t1", seat="eng-9@sw", started_at=1, hive="hive-c")
    rig.store.start_execution(SLUG, earlier, "")
    for hive in ("hive-b", "hive-c"):
        copy = {"progress.md": f"- step on {hive}\n"}
        rig.store.redis.set(task_copy(hive), json.dumps(copy))
    rig.store.redis.set(rig.store.key(SLUG, "work-folder", "t1"), "hive-b")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"][orders.SEEDS]["progress.md"] == "- step on hive-b\n"


@pytest.mark.feature("work-folder-follows-the-task-across-hives")
def test_the_seeds_fall_back_to_the_previous_agents_hive_when_the_launch_hive_holds_no_copy(
    rig: SimpleNamespace,
) -> None:
    from scripts.hive import orders
    from scripts.swarm.store import AgentRecord

    named = rig.store.next_name(SLUG, "eng", 1)
    earlier = AgentRecord(named, "eng", "t1", seat="eng-9@sw", started_at=1, hive="hive-c")
    rig.store.start_execution(SLUG, earlier, "")
    rig.store.redis.set(task_copy("hive-c"), json.dumps({"progress.md": "- step on hive-c\n"}))
    rig.store.redis.set(rig.store.key(SLUG, "work-folder", "t1"), "hive-b")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted["task"][orders.SEEDS]["progress.md"] == "- step on hive-c\n"


@pytest.mark.feature("work-folder-follows-the-task-across-hives")
def test_a_successor_on_another_hive_after_a_roll_receives_every_line(rig: SimpleNamespace, tmp_path: Any) -> None:
    from dataclasses import replace
    from pathlib import Path
    from unittest.mock import Mock, patch

    from scripts.hive import lanes, orders

    homes = {hive: tmp_path / hive for hive in ("hive-a", "hive-b")}
    for home in homes.values():
        home.mkdir()

    def on(hive: str) -> Any:
        return patch.object(Path, "home", return_value=homes[hive])

    def beat() -> None:
        orders.Consumer(rig.store.redis, "hive-a", Mock()).sync_workspaces(45)

    first = rig.admitter.admit(SLUG, ask())
    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True
    with on("hive-a"):
        folder = Path(lanes.placed(SLUG, first["task"])["workspace"])
        (folder / "progress.md").write_text("- step one\n", encoding="utf-8")
        (folder / "proof.md").write_text("- proof one\n", encoding="utf-8")
        beat()
        with (folder / "progress.md").open("a", encoding="utf-8") as fh:
            fh.write("- step written just before the roll\n")
        agent = rig.store.execution_registry.get(SLUG, first["task"]["execution_id"])
        rig.store.execution_registry.update(SLUG, replace(agent, state="finished"))
        beat()
        expected = {name: (folder / name).read_text(encoding="utf-8") for name in ("progress.md", "proof.md")}
    rig.store.release(SLUG, "t1", first["name"])
    rig.store.drop_agent(SLUG, first["name"])
    with on("hive-a"):
        beat()
    rig.rows["t1"].update(state="open", claimed_by="")

    second = rig.admitter.admit(SLUG, ask(hive="hive-b", generation=1))

    with on("hive-b"):
        moved = Path(lanes.placed(SLUG, second["task"])["workspace"])
        assert {name: (moved / name).read_text(encoding="utf-8") for name in expected} == expected
        assert (moved / "steering.md").read_text(encoding="utf-8") == "# t1: title t1\n"


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

    assert len(read) == 1
    assert read[0]["overview"] == rig.found["overview"]
    assert [phase["id"] for phase in read[0]["phases"]] == ["p1"]
    assert "t1" in {row["id"] for row in read[0]["tasks"]}
