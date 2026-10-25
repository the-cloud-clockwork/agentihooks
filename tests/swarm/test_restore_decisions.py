import pytest

from scripts.handoff import transfers
from scripts.swarm import resume, snapshot
from scripts.swarm.tick import tick
from tests.swarm.test_resume import ResumingRuntime, restore, saved, store
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.unit
_fixture = store


def test_failed_resume_stays_awaiting_decision_even_after_a_tick(store, tmp_path):
    saved(store, tmp_path)
    runtime = ResumingRuntime(fail="cannot reopen conversation")
    outcomes = restore(store, runtime)
    assert {o.outcome for o in outcomes} == {"awaiting-decision"}
    assert {a.state for a in store.agents("sw")} == {"awaiting-decision"}
    tick("sw", store, FakeLedger([{"id": "t1", "state": "claimed", "claimed_by": "sw-eng-1"}]), runtime, 999_999)
    assert not runtime.spawned
    assert not runtime.masters
    assert len(store.agents("sw")) == 2
    assert (
        next(a for a in store.agents("sw") if a.name == "sw-eng-1").conversation_id
        == "0f8e6d4c-1111-2222-3333-444455556666"
    )
    assert {r["binding"]["state"] for r in transfers.list_transfers(store, "sw")} == {"absent"}


def test_retry_resume_and_explicit_fresh_are_separate_choices(store, tmp_path):
    saved(store, tmp_path)
    restore(store, ResumingRuntime(fail="unavailable"))
    runtime = ResumingRuntime()
    ledger = FakeLedger([{"id": "t1", "state": "claimed", "claimed_by": "sw-eng-1"}])
    resumed = resume.decide(store, "sw", "sw-eng-1", "resume", runtime, 2000, ledger)
    assert resumed.outcome == "resumed"
    assert runtime.resumed[0][:2] == ("sw-eng-1", "0f8e6d4c-1111-2222-3333-444455556666")
    assert snapshot.ledger_source("sw") in runtime.resumed[0][2]
    eng = next(a for a in store.agents("sw") if a.name == "sw-eng-1")
    assert (eng.state, eng.started_at) == ("working", 2000)
    fresh = resume.decide(store, "sw", "sw-master-1", "fresh", runtime, 2001, ledger)
    assert fresh.outcome == "fresh"
    assert next(a for a in store.agents("sw") if a.lane == "master").state == "finished"
    assert next(r for r in store.restored("sw") if r["name"] == "sw-master-1")["outcome"] == "fresh"


def test_failed_retry_keeps_the_decision_and_never_spawns_fresh(store, tmp_path):
    saved(store, tmp_path)
    restore(store, ResumingRuntime(fail="unavailable"))
    runtime = ResumingRuntime(fail="still unavailable")
    result = resume.decide(store, "sw", "sw-eng-1", "resume", runtime, 2000, FakeLedger([]))
    assert result.outcome == "awaiting-decision"
    assert "still unavailable" in result.reason
    assert not runtime.spawned


def test_restored_session_can_confirm_before_the_launcher_returns(store, tmp_path, monkeypatch):
    from dataclasses import replace

    from scripts.swarm.store import AgentRecord, SwarmConfig

    store.create(SwarmConfig("sw", str(tmp_path), 0, 0))
    agent = AgentRecord("sw-master-1", "master", "master", seat="master@sw", conversation_id="saved-conversation")
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, agent.name, 1)
    document = "# Handoff v2\n## Next\nRead the saved proof.\n## Read first\nNone\n"
    store.put_handoff("sw", "master", document, seat=agent.seat)
    runtime = ResumingRuntime()
    launch = runtime.resume

    def confirm_during_launch(config, current, text):
        row = transfers.list_transfers(store, "sw")[-1]
        transfers.confirm(store, "sw", row["id"], replace(current, state="working"), row["next"], 3)
        return launch(config, current, text)

    monkeypatch.setattr(runtime, "resume", confirm_during_launch)
    results = resume.reopen(store, "sw", {}, runtime, 2, tmp_path, has_quota=lambda *_: True)
    assert results[0].outcome == "resumed"
    assert transfers.list_transfers(store, "sw")[-1]["continuity"]["state"] == "confirmed"


class _Hashes:
    def __init__(self):
        self.rows = {}

    def hset(self, key, field, value):
        self.rows.setdefault(key, {})[field] = value


class _PodStore:
    def __init__(self, agent):
        self.agent, self.redis = agent, _Hashes()

    def agents(self, slug):
        return [self.agent]

    def key(self, slug, name):
        return f"swarm:{slug}:{name}"

    def restored(self, slug):
        raise AssertionError("a Pod attempt never reads the local restore list")


def _pod_agent(**changes):
    from dataclasses import replace

    from scripts.swarm.store import AgentRecord

    agent = AgentRecord("sw-eng-1", "eng", "t1", conversation_id="s-1", state="awaiting-decision")
    return replace(agent, **{"execution_id": "exe-1", "runtime_backend": "kubernetes", **changes})


@pytest.mark.parametrize("choice", ["resume", "fresh"])
def test_a_pod_attempt_choice_is_requested_for_the_controller_and_nothing_starts_here(choice):
    pods = _PodStore(_pod_agent())
    runtime = ResumingRuntime()
    outcome = resume.decide(pods, "sw", "sw-eng-1", choice, runtime, 5000, FakeLedger([]))
    assert outcome == resume.Outcome(
        "sw-eng-1", "eng", "t1", "awaiting-decision", f"Recovery requested: {choice}", "s-1", 5000
    )
    assert pods.redis.rows == {"swarm:sw:recovery-requests": {"exe-1": choice}}
    assert not runtime.spawned
    assert not runtime.resumed
    again = resume.decide(pods, "sw", "sw-eng-1", choice, runtime, 5001, FakeLedger([]))
    assert again.at == 5001
    assert pods.redis.rows == {"swarm:sw:recovery-requests": {"exe-1": choice}}


def test_a_pod_attempt_refuses_an_unknown_choice_and_requests_nothing():
    pods = _PodStore(_pod_agent())
    with pytest.raises(resume.SwarmError, match="^Choose native, context, fresh or resume$"):
        resume.decide(pods, "sw", "sw-eng-1", "recovery_pending", ResumingRuntime(), 5000, FakeLedger([]))
    assert pods.redis.rows == {}


@pytest.mark.parametrize("choice", ["native", "context"])
def test_a_pod_attempt_requests_an_explicit_continuation(choice):
    pods = _PodStore(_pod_agent())
    outcome = resume.decide(pods, "sw", "sw-eng-1", choice, ResumingRuntime(), 5000, FakeLedger([]))
    assert outcome.reason == f"Recovery requested: {choice}"
    assert pods.redis.rows == {"swarm:sw:recovery-requests": {"exe-1": choice}}


def test_a_local_agent_has_no_context_continuation_and_stays_awaiting(store, tmp_path):
    saved(store, tmp_path)
    restore(store, ResumingRuntime(fail="unavailable"))
    with pytest.raises(resume.SwarmError, match=f"^{resume.LOCAL_CONTEXT}$"):
        resume.decide(store, "sw", "sw-eng-1", "context", ResumingRuntime(), 2000, FakeLedger([]))
    with pytest.raises(resume.SwarmError, match="^Choose native, context, fresh or resume$"):
        resume.decide(store, "sw", "sw-eng-1", "later", ResumingRuntime(), 2000, FakeLedger([]))
    assert {a.state for a in store.agents("sw")} == {"awaiting-decision"}


def test_native_reopens_a_local_conversation_and_records_the_decision(store, tmp_path):
    saved(store, tmp_path)
    restore(store, ResumingRuntime(fail="unavailable"))
    awaited = next(r for r in store.restored("sw") if r["name"] == "sw-eng-1")["at"]
    runtime = ResumingRuntime()
    outcome = resume.decide(store, "sw", "sw-eng-1", "native", runtime, 2000, FakeLedger([]))
    assert (outcome.outcome, outcome.decided, outcome.awaited_at) == ("resumed", True, awaited)
    assert runtime.resumed[0][0] == "sw-eng-1"
    row = next(r for r in store.restored("sw") if r["name"] == "sw-eng-1")
    assert (row["decided"], row["awaited_at"], row["at"]) == (True, awaited, 2000)


def test_a_failed_retry_is_not_a_decision_and_keeps_its_first_wait(store, tmp_path):
    saved(store, tmp_path)
    restore(store, ResumingRuntime(fail="unavailable"))
    awaited = next(r for r in store.restored("sw") if r["name"] == "sw-eng-1")["at"]
    resume.decide(store, "sw", "sw-eng-1", "resume", ResumingRuntime(fail="again"), 2000, FakeLedger([]))
    resume.decide(store, "sw", "sw-eng-1", "resume", ResumingRuntime(fail="again"), 3000, FakeLedger([]))
    row = next(r for r in store.restored("sw") if r["name"] == "sw-eng-1")
    assert (row["decided"], row["awaited_at"], row["at"]) == (False, awaited, 3000)


def test_native_is_refused_for_a_lost_conversation_before_anything_is_attached(store, tmp_path):
    saved(store, tmp_path, conversation="")
    restore(store, ResumingRuntime())
    before = (store.restored("sw"), transfers.list_transfers(store, "sw"), store.seats.occupant("eng-1@sw"))
    runtime = ResumingRuntime()
    with pytest.raises(resume.SwarmError, match="^no conversation id$"):
        resume.decide(store, "sw", "sw-eng-1", "native", runtime, 2000, FakeLedger([]))
    assert (store.restored("sw"), transfers.list_transfers(store, "sw"), store.seats.occupant("eng-1@sw")) == before
    assert not runtime.resumed
    assert {a.state for a in store.agents("sw") if a.name == "sw-eng-1"} == {"awaiting-decision"}


@pytest.mark.parametrize("changes", [{"runtime_backend": "local"}, {"execution_id": ""}])
def test_only_a_pod_attempt_takes_the_controller_path(changes):
    pods = _PodStore(_pod_agent(**changes))
    with pytest.raises(AssertionError, match="local restore list"):
        resume.decide(pods, "sw", "sw-eng-1", "fresh", ResumingRuntime(), 5000, FakeLedger([]))
    assert (resume.KUBERNETES, resume.RECOVERY_REQUESTS) == ("kubernetes", "recovery-requests")


def test_native_is_not_refused_for_an_account_out_of_quota(store, tmp_path, monkeypatch):
    saved(store, tmp_path)
    restore(store, ResumingRuntime(fail="unavailable"))
    monkeypatch.setattr(resume.agent_choice, "account_has_quota", lambda *_: False)
    outcome = resume.decide(store, "sw", "sw-eng-1", "native", ResumingRuntime(), 2000, FakeLedger([]))
    assert outcome.name == "sw-eng-1"
