import pytest

from scripts.handoff import transfers
from scripts.swarm import resume
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
    assert next(a for a in store.agents("sw") if a.name == "sw-eng-1").state == "working"
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
