import json

import pytest

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import proof_swarm

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
SLUG = "proof-323133-t351-1"
SETTING = json.dumps({"agent": "engineer@323133-1068", "task": "t351"})
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
ENV = {
    "AGENTIHOOKS_CONTROL_PROOF": SETTING,
    "AGENTIHOOKS_LAUNCH_PROJECTS": f"{PROJECT},github.com/the-cloud-clockwork/antoncore",
    "LEDGER_URL": "http://ledger:8765",
}


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


class Ledger:
    def __init__(self, named=SLUG):
        self.named, self.created, self.removed = named, [], []

    def create(self, content, environ):
        self.created.append((content, environ))
        return {"slug": self.named, "created": True}

    def remove(self, slug, environ):
        self.removed.append((slug, environ))
        return {"slug": slug, "removed_at": 1}


def test_a_set_proof_creates_its_ledger_and_a_one_engineer_swarm(store):
    ledger = Ledger()

    assert proof_swarm.reconcile(ENV, store, SLUG, ledger) == (
        f"controller: created proof swarm {SLUG} with one engineer task"
    )
    ((content, environ),) = ledger.created
    assert (environ["AGENTIHOOKS_AGENT_NAME"], environ["AGENTIHOOKS_SWARM_TASK"]) == ("engineer@323133-1068", "t351")
    assert environ["LEDGER_URL"] == "http://ledger:8765"
    assert content == proof_swarm.CONTENT
    assert [task["lane"] for task in content["tasks"]] == ["eng"]
    assert [task["phase"] for task in content["tasks"]] == ["p1"]
    config = store.config(SLUG)
    assert (config.repo, config.max_eng, config.max_ci, config.state) == (PROJECT, 1, 0, "running")
    assert store.redis.get(proof_swarm.RECORD) == SLUG


def test_a_recorded_proof_is_kept_across_restarts(store):
    ledger = Ledger()
    proof_swarm.reconcile(ENV, store, SLUG, ledger)

    assert proof_swarm.reconcile(ENV, store, SLUG, ledger) == f"controller: kept proof swarm {SLUG}"
    assert len(ledger.created) == 1


def test_a_proof_named_other_than_the_served_swarm_is_removed_and_refused(store):
    ledger = Ledger("proof-323133-t351-2")

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(ENV, store, SLUG, ledger)

    assert str(refused.value) == f"the proof ledger is named proof-323133-t351-2 but the controller serves {SLUG}"
    assert [slug for slug, _ in ledger.removed] == ["proof-323133-t351-2"]
    assert store.slugs() == []
    assert store.redis.get(proof_swarm.RECORD) is None


def test_a_cleared_setting_removes_the_recorded_proof(store):
    ledger = Ledger()
    proof_swarm.reconcile(ENV, store, SLUG, ledger)
    cleared = {key: value for key, value in ENV.items() if key != "AGENTIHOOKS_CONTROL_PROOF"}

    assert proof_swarm.reconcile(cleared, store, "rig-grade-swarm", ledger) == f"controller: removed proof swarm {SLUG}"
    assert ledger.removed == [(SLUG, cleared)]
    assert store.slugs() == []
    assert store.redis.get(proof_swarm.RECORD) is None


def test_a_cleared_setting_finishes_a_removal_whose_swarm_is_already_gone(store):
    store.redis.set(proof_swarm.RECORD, SLUG)
    ledger = Ledger()

    assert proof_swarm.reconcile({}, store, "rig-grade-swarm", ledger) == f"controller: removed proof swarm {SLUG}"
    assert [slug for slug, _ in ledger.removed] == [SLUG]
    assert store.redis.get(proof_swarm.RECORD) is None


def test_nothing_happens_without_a_setting_or_a_record(store):
    ledger = Ledger()

    assert proof_swarm.reconcile({}, store, "rig-grade-swarm", ledger) == ""
    assert (ledger.created, ledger.removed) == ([], [])
