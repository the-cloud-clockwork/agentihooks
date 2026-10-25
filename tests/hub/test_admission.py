import json
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


def test_the_hub_admits_an_offer_by_claiming_starting_seating_and_marking_it_claimed(rig):
    admitted = rig.admitter.admit(SLUG, ask())

    name = admitted["name"]
    assert rig.store.claimant(SLUG, "t1") == name
    [agent] = rig.store.agents(SLUG)
    assert (agent.name, agent.lane, agent.task, agent.seat, agent.hive) == (name, "eng", "t1", "eng-1@sw", "hive-a")
    assert (agent.state, agent.started_at) == ("starting", 7_000)
    assert rig.store.redis.hget(rig.store.names.key("name", name), "spawned_at") == "7000"
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
