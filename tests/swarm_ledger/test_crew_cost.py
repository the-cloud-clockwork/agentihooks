from scripts.swarm import naming
from scripts.swarm_ledger import ledger_gate


def operator(rev, kind="comment", target="phases/p1", **extra):
    return {"rev": rev, "by": "operator", "kind": kind, "target": target, **extra}


def test_unhandled_for_resolves_the_name_once_however_many_events(monkeypatch):
    calls = []
    monkeypatch.setattr(naming, "resolve_name", lambda name: calls.append(name) or name)
    meta = {
        "members": {"eng": {"handled_rev": 0}, "boss": {"role": "orchestrator"}},
        "events": [operator(n) for n in range(1, 201)],
    }
    assert len(ledger_gate.unhandled_for(meta, "boss")) == 200
    assert ledger_gate.unhandled_for(meta, "eng") == []
    assert len(calls) == 2


def test_crew_finds_each_event_owner_once_for_every_member(monkeypatch):
    monkeypatch.setattr(naming, "resolve_name", lambda name: name)
    looked = []
    owner = ledger_gate.owner
    monkeypatch.setattr(ledger_gate, "owner", lambda event, *args: looked.append(event["rev"]) or owner(event, *args))
    members = {f"eng{n}": {"handled_rev": 0} for n in range(5)}
    members["eng4"]["handled_rev"] = 2
    events = [
        operator(1, "chat", "chat", text="@eng3 look"),
        operator(2, "sync requested", "chat", text="sync"),
        operator(3),
        {"rev": 4, "by": "eng0", "kind": "comment", "target": "phases/p1"},
        operator(5, "chat cleared", "chat"),
    ]
    crew = ledger_gate.crew({"members": members, "events": events})
    assert {member["name"]: member["unhandled"] for member in crew} == {
        "eng0": 2,
        "eng1": 2,
        "eng2": 2,
        "eng3": 3,
        "eng4": 1,
    }
    assert sorted(looked) == [1, 2, 3]


def test_unhandled_for_routes_owned_events_only_to_their_owner(monkeypatch):
    monkeypatch.setattr(naming, "resolve_name", lambda name: name)
    members = {"boss": {"role": "orchestrator"}, "eng": {}}
    tasks = [{"id": "t1", "claimed_by": "eng"}]
    events = [operator(1, target="tasks/t1"), operator(2), operator(3, "sync requested", "chat")]
    meta = {"members": members, "events": events}
    assert [e["rev"] for e in ledger_gate.unhandled_for(meta, "eng", tasks)] == [1, 3]
    assert [e["rev"] for e in ledger_gate.unhandled_for(meta, "boss", tasks)] == [2, 3]
    assert ledger_gate.unhandled_for(meta, "stranger", tasks) == []


def test_a_ledger_without_events_owes_nothing(monkeypatch):
    monkeypatch.setattr(naming, "resolve_name", lambda name: name)
    assert ledger_gate.unhandled_for({"members": {"eng": {}}}, "eng") == []
    assert ledger_gate.crew({"members": {"eng": {}}})[0]["unhandled"] == 0
