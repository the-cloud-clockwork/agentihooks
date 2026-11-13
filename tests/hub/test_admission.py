import json
from collections.abc import Callable
from types import SimpleNamespace

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
    yield SimpleNamespace(store=store, ledger=ledger, rows=rows, admitter=admitter)
    store.redis.close()


def ask(**fields):
    return {"hive": "hive-a", "task": "t1", "lane": "eng", "generation": 0, **fields}


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


@pytest.mark.parametrize("lives, mode", [(2, "enforce"), (3, "observe"), (3, "off")])
def test_events_admission_preserves_the_third_life_and_nonblocking_gate_modes(rig, lives, mode):
    rig.store.update(SLUG, gates={"claims": mode})
    for _ in range(lives):
        rig.store.count_claim(SLUG, "t1")

    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None
    assert rig.rows["t1"]["state"] == "claimed"
    assert rig.store.claims(SLUG, "t1") == lives


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
    assert rig.ledger.writes == [
        (SLUG, "t1", {"state": "claimed", "claimed_by": name}, ("open",), {"claimed_by": "", "claim_generation": 0})
    ]
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


def test_a_task_with_a_pull_request_is_admitted_back_into_pr(rig):
    admitted = rig.admitter.admit(SLUG, ask(task="t2", lane="ci"))
    assert rig.rows["t2"]["state"] == "pr"
    assert admitted["task"]["seat"] == "ci-1@sw"


def test_a_task_returns_to_the_seat_its_handoff_names(rig):
    rig.store.put_handoff(SLUG, "t1", "carry on", seat="eng-3@sw")
    assert rig.admitter.admit(SLUG, ask())["task"]["seat"] == "eng-3@sw"


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


def test_a_ledger_that_refuses_the_claim_releases_the_claim_and_the_agent(rig):
    rig.ledger.update_task = lambda *args, **kwargs: {**rig.rows["t1"], "state": "claimed", "claimed_by": "other"}
    assert rig.admitter.admit(SLUG, ask()) is None
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []


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
    assert admission.OPERATIONS == ("admit", "reopen", "failed")


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
