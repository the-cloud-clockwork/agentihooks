import ledger_gate
import ledger_hook


def test_a_relay_counts_as_ledger_progress():
    assert "relay" in ledger_gate.WRITE_COMMANDS
    assert ledger_hook.touched('agentihooks ledger --slug s --as m relay questions/q "Use it." --quote "use it"')
    assert not ledger_hook.touched("agentihooks ledger --slug s --as m status")


def test_unhandled_for_resolves_the_name_once_however_many_events(monkeypatch):
    from scripts.swarm import naming

    calls = []
    monkeypatch.setattr(naming, "resolve_name", lambda name: calls.append(name) or name)
    meta = {
        "members": {"eng": {"handled_rev": 0}, "boss": {"role": "orchestrator"}},
        "events": [{"rev": n, "by": "operator", "kind": "comment", "target": "phases/p1"} for n in range(1, 201)],
    }
    assert len(ledger_gate.unhandled_for(meta, "boss")) == 200
    assert ledger_gate.unhandled_for(meta, "eng") == []
    assert len(calls) == 2


def test_crew_finds_each_event_owner_once_for_every_member(monkeypatch):
    from scripts.swarm import naming

    monkeypatch.setattr(naming, "resolve_name", lambda name: name)
    looked = []
    owner = ledger_gate.owner
    monkeypatch.setattr(ledger_gate, "owner", lambda event, *args: looked.append(event["rev"]) or owner(event, *args))
    members = {f"eng{n}": {"handled_rev": 0} for n in range(5)}
    events = [{"rev": 1, "by": "operator", "kind": "chat", "target": "chat", "text": "@eng3 look"}]
    events.append({"rev": 2, "by": "operator", "kind": "sync requested", "target": "chat", "text": "sync"})
    crew = ledger_gate.crew({"members": members, "events": events})
    assert {member["name"]: member["unhandled"] for member in crew} == {
        "eng0": 1,
        "eng1": 1,
        "eng2": 1,
        "eng3": 2,
        "eng4": 1,
    }
    assert sorted(looked) == [1, 2]
