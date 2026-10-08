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
    monkeypatch.setattr(naming, "resolve_names", lambda names: {name: name for name in names})
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
    monkeypatch.setattr(naming, "resolve_names", lambda names: {name: name for name in names})
    assert ledger_gate.crew({"members": {"eng": {}}})[0]["unhandled"] == 0


class Events(list):
    passes = 0

    def __iter__(self):
        Events.passes += 1
        return super().__iter__()


def test_crew_resolves_every_member_in_one_call_and_reads_the_events_once(monkeypatch):
    batches = []
    monkeypatch.setattr(naming, "resolve_name", lambda name: (_ for _ in ()).throw(AssertionError(name)))
    monkeypatch.setattr(naming, "resolve_names", lambda names: batches.append(list(names)) or {n: n for n in names})
    members = {f"eng{n}": {"handled_rev": 0} for n in range(50)}
    Events.passes = 0
    events = Events([operator(1), {"rev": 2, "by": "eng0", "kind": "comment", "target": "phases/p1"}, operator(3)])
    crew = ledger_gate.crew({"members": members, "events": events})
    assert batches == [list(members)]
    assert Events.passes == 1
    assert {member["unhandled"] for member in crew} == {2}


def test_crew_counts_a_renamed_member_by_the_name_it_resolves_to(monkeypatch):
    monkeypatch.setattr(naming, "resolve_names", lambda names: {n: "new" if n == "old" else n for n in names})
    members = {"old": {"handled_rev": 0}, "new": {"handled_rev": 3}, "eng": {"handled_rev": 1}}
    events = [operator(1), operator(2), operator(3, "sync requested", "chat", text="sync")]
    crew = ledger_gate.crew({"members": members, "events": events})
    assert [(m["name"], m["handled_rev"], m["unhandled"]) for m in crew] == [
        ("old", 0, 0),
        ("new", 3, 0),
        ("eng", 1, 2),
    ]
