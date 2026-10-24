import json

import pytest

from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import proof_swarm
from tests.test_swarm_v2_proof_swarm import ENV, PROJECT, SLUG, Ledger

pytestmark = pytest.mark.unit
LIVE = "rig-grade-swarm"
SCRATCH = "proof-323133-t1199-1"
OTHER_SCRATCH = "proof-323133-t1199-2"
ENTRY = {"swarm": SCRATCH, "agent": "engineer@323133-3110", "task": "t1199", "repo": PROJECT}
CLEARED = {key: value for key, value in ENV.items() if key != proof_swarm.PROOF_ENV}


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def scratch(*entries):
    return {**CLEARED, proof_swarm.SCRATCH_ENV: json.dumps(list(entries))}


def test_a_listed_scratch_swarm_is_created_beside_the_live_swarm(store):
    store.create(SwarmConfig(LIVE, PROJECT, 16, 4))
    ledger = Ledger(named=SCRATCH)

    assert proof_swarm.reconcile(scratch(ENTRY), store, "", ledger) == [
        f"controller: created proof swarm {SCRATCH} with one engineer task"
    ]
    ((content, environ),) = ledger.created
    assert (environ["AGENTIHOOKS_AGENT_NAME"], environ["AGENTIHOOKS_SWARM_TASK"]) == ("engineer@323133-3110", "t1199")
    assert content == proof_swarm.CONTENT
    config = store.config(SCRATCH)
    assert (config.repo, config.scratch, config.max_eng, config.max_ci) == (PROJECT, True, 1, 0)
    assert sorted(store.slugs()) == [LIVE, SCRATCH]
    assert store.config(LIVE).max_eng == 16


def test_every_listed_scratch_swarm_is_kept_beside_the_served_proof(store):
    for slug in (SLUG, SCRATCH, OTHER_SCRATCH):
        store.create(SwarmConfig(slug, PROJECT, 1, 0))
    ledger = Ledger()
    environ = {**ENV, proof_swarm.SCRATCH_ENV: json.dumps([ENTRY, {**ENTRY, "swarm": OTHER_SCRATCH}])}

    assert proof_swarm.reconcile(environ, store, SLUG, ledger) == []
    assert (ledger.created, ledger.removed) == ([], [])
    assert sorted(store.slugs()) == sorted([SLUG, SCRATCH, OTHER_SCRATCH])


def test_a_scratch_swarm_dropped_from_the_list_is_removed_and_the_live_swarm_kept(store):
    store.create(SwarmConfig(LIVE, PROJECT, 16, 4))
    store.create(SwarmConfig(SCRATCH, PROJECT, 1, 0))
    ledger = Ledger(live={SCRATCH})

    assert proof_swarm.reconcile(scratch(), store, "", ledger) == [f"controller: removed proof swarm {SCRATCH}"]
    assert ledger.removed == [(SCRATCH, scratch())]
    assert store.slugs() == [LIVE]


def test_a_listed_scratch_swarm_whose_ledger_takes_another_name_is_refused(store):
    ledger = Ledger(named=OTHER_SCRATCH)

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(scratch(ENTRY), store, "", ledger)

    assert str(refused.value) == f"the proof ledger is named {OTHER_SCRATCH} but the controller serves {SCRATCH}"
    assert ledger.removed == [(OTHER_SCRATCH, scratch(ENTRY))]
    assert store.slugs() == []


@pytest.mark.parametrize(
    "setting",
    [
        "not json",
        json.dumps({"swarm": SCRATCH}),
        json.dumps([{**ENTRY, "agent": ""}]),
        json.dumps([{**ENTRY, "task": ""}]),
        json.dumps([{**ENTRY, "repo": ""}]),
        json.dumps([{**ENTRY, "swarm": LIVE}]),
        json.dumps(["proof-323133-t1199-1"]),
    ],
)
def test_a_malformed_scratch_list_is_refused_without_touching_any_swarm(store, setting):
    store.create(SwarmConfig(LIVE, PROJECT, 16, 4))
    ledger = Ledger()

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile({**CLEARED, proof_swarm.SCRATCH_ENV: setting}, store, "", ledger)

    assert str(refused.value) == proof_swarm.SCRATCH_MALFORMED
    assert (ledger.asked, ledger.created, ledger.removed) == ([], [], [])
    assert store.slugs() == [LIVE]


def test_the_served_proof_may_not_also_be_listed_as_a_scratch_swarm(store):
    ledger = Ledger()

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(
            {**ENV, proof_swarm.SCRATCH_ENV: json.dumps([{**ENTRY, "swarm": SLUG}])}, store, SLUG, ledger
        )

    assert str(refused.value) == f"{SLUG} is the served proof swarm and may not be listed as a scratch swarm"
    assert (ledger.asked, ledger.created) == ([], [])
