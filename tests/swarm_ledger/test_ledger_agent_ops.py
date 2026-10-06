import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_agent_ops, ledger_core  # noqa: E402


def meta():
    member = {"joined_at": 1, "last_seen": 1, "claims": [], "handled_rev": 0}
    members = {"boss": {**member, "role": "orchestrator"}, "eng": {**member, "role": "member"}}
    return {"rev": 0, "stamps": {}, "events": [], "members": members}


def step(state, kind, **fields):
    ctx = ledger_core.Context(state, state["rev"] * 10)
    if kind == "sync":
        ctx.record("operator", "stats sync requested", "", id=fields["id"])
        done = True
    else:
        done = ledger_agent_ops.apply({}, {"op": "ack", "id": "a", "by": kind, "rev": fields["rev"]}, ctx)
    state["events"] += ctx.events
    state["rev"] = ctx.rev
    return done, ctx


def answers(state):
    return [(e["by"], e["kind"], e["target"], e["id"], e["at"]) for e in state["events"] if e["by"] != "operator"]


def test_the_orchestrators_ack_of_the_check_records_one_answer_at_the_ack_time():
    state = meta()
    step(state, "sync", id="s1")
    done, ctx = step(state, "boss", rev=1)
    assert (done, ctx.dirty, state["members"]["boss"]["handled_rev"]) == (True, True, 1)
    assert answers(state) == [("boss", "stats check answered", "", "s1", 10)]
    step(state, "boss", rev=2)
    assert answers(state) == [("boss", "stats check answered", "", "s1", 10)]


def test_a_members_ack_and_an_ack_before_the_check_record_nothing():
    state = meta()
    step(state, "boss", rev=0)
    step(state, "sync", id="s1")
    step(state, "eng", rev=2)
    step(state, "boss", rev=1)
    assert answers(state) == []
    assert state["members"]["boss"]["handled_rev"] == 1


def test_each_new_check_gets_its_own_answer_and_only_the_latest_is_answered():
    state = meta()
    step(state, "sync", id="s1")
    step(state, "boss", rev=1)
    step(state, "sync", id="s2")
    step(state, "sync", id="s3")
    step(state, "boss", rev=4)
    assert [a[3] for a in answers(state)] == ["s1", "s3"]


def test_an_ack_from_someone_who_never_joined_changes_nothing():
    state = meta()
    step(state, "sync", id="s1")
    done, ctx = step(state, "ghost", rev=1)
    assert (done, ctx.dirty, answers(state)) == (False, False, [])
