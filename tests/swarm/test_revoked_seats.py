import json
from dataclasses import replace

import pytest

from scripts.handoff import transfers
from scripts.swarm import revoked_seats
from scripts.swarm.store import MASTER
from scripts.swarm.tick import _watch_idle, tick
from tests.swarm.test_master_stall import START, SlugLedger, master
from tests.swarm.test_outage_resume import three_owners
from tests.swarm.test_outage_seat_release import executing
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit


def revoke(store, *accounts, harness="claude", flag=True):
    rows = [
        {
            "harness": harness,
            "name": name,
            "state": "CLOSED",
            "sessions": 1,
            "five_left": None,
            "week_left": None,
            "cap": 0,
            **({"revoked": True} if flag else {}),
        }
        for name in accounts
    ]
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"accounts": rows}))


def test_the_revoked_accounts_are_read_from_the_capacity_rows(store):
    assert revoked_seats.accounts(store, "sw") == set()
    revoke(store, "gone", "lost")
    assert revoked_seats.accounts(store, "sw") == {("claude", "gone"), ("claude", "lost")}
    revoke(store, "closed", flag=False)
    assert revoked_seats.accounts(store, "sw") == set()


def test_every_worker_on_a_revoked_account_is_retired_and_its_task_reopened(store):
    ledger, runtime, agents = three_owners(store)
    gone = [executing(store, replace(agent, account="gone")) for agent in agents[:2]]
    kept = executing(store, replace(agents[2], account="gone", harness="codex"))
    runtime.statuses[kept.name] = "working"
    claims = {agent.task: store.claims("sw", agent.task) for agent in gone}
    revoke(store, "gone")
    actions = tick("sw", store, ledger, runtime, 2_000)
    assert sorted(runtime.killed) == sorted(agent.name for agent in gone)
    rows = {row["predecessor"]: row for row in transfers.list_transfers(store, "sw")}
    for agent in gone:
        assert f"freed the seat of {agent.name}: claude account gone was revoked, task {agent.task} reopened" in actions
        assert ledger.rows[agent.task]["state"] == "open"
        assert store.claimant("sw", agent.task) is None
        assert store.claims("sw", agent.task) == claims[agent.task] - 1
        row = rows[agent.name]
        assert (row["reason"], row["task"], row["seat"], row["at"]) == ("revoked", agent.task, agent.seat, 2_000)
        assert "avoid_harness" not in row
        assert "the provider revoked the token of claude account gone" in row["handoff"]
        assert f"workspace:{agent.task}/progress" in row["handoff"]
        assert row["handoff"].endswith("<!-- handoff complete -->\n")
    assert kept.name not in runtime.killed
    assert store.claimant("sw", kept.task) == kept.name
    assert set(rows) == {agent.name for agent in gone}


def test_a_worker_that_will_not_retire_keeps_its_claim_until_the_next_tick(store):
    ledger, runtime, agents = three_owners(store)
    owner = executing(store, replace(agents[0], account="gone"))
    for agent in agents[1:]:
        runtime.statuses[agent.name] = "working"
    revoke(store, "gone")
    runtime.stuck.add(owner.name)
    actions = tick("sw", store, ledger, runtime, 2_000)
    assert f"could not retire {owner.name} on revoked claude account gone, retrying next tick" in actions
    assert store.claimant("sw", owner.task) == owner.name
    assert not transfers.list_transfers(store, "sw")
    runtime.stuck.remove(owner.name)
    tick("sw", store, ledger, runtime, 3_000)
    assert owner.name in runtime.killed
    assert store.claimant("sw", owner.task) is None


def test_an_existing_handoff_carries_over_to_the_successor(store):
    ledger, runtime, agents = three_owners(store)
    owner = executing(store, replace(agents[0], account="gone"))
    text = "## Intent\nKept\n\n<!-- handoff complete -->\n"
    store.put_handoff("sw", owner.task, text, seat=owner.seat)
    row = revoked_seats.worker_handoff(store, "sw", owner, 5_000)
    assert (row["reason"], row["handoff"], row["at"]) == ("revoked", text, 5_000)


def test_an_agent_without_an_account_is_never_on_a_revoked_one(store):
    revoke(store, "")
    agent, _ = master(store)
    assert not revoked_seats.on_revoked(store, "sw", agent)


def test_a_master_on_a_revoked_account_is_recycled_in_its_seat(store):
    agent, runtime = master(store)
    agent = replace(agent, account="gone")
    store.put_agent("sw", agent)
    revoke(store, "gone")
    found = _watch_idle("sw", store, SlugLedger([]), runtime, {}, agent, START + 60_000)
    assert found == [f"recycled master {agent.name} in its seat: claude account gone was revoked"]
    assert [a.state for a in store.agents("sw")] == ["finished"]
    handoff = store.handoff("sw", MASTER)
    assert "Continue as the master of sw; the provider revoked the token of claude account gone" in handoff
    assert "inbox:master" in handoff and handoff.endswith("<!-- handoff complete -->\n")
    assert store.handoff_seat("sw", MASTER) == agent.seat
    assert store.handoff_envelope("sw", MASTER)["reason"] == "recycle"
    [row] = transfers.list_transfers(store, "sw")
    assert (row["reason"], row["predecessor"], row["seat"], row["at"]) == (
        "revoked",
        agent.name,
        agent.seat,
        START + 60_000,
    )
    assert runtime.killed == []
