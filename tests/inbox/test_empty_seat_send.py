import json

import fakeredis

from scripts.inbox import cli
from scripts.inbox.store import InboxStore
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


def test_send_to_an_empty_seat_warns_and_names_only_live_overlapping_owners(monkeypatch, capsys):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/r", 3, 2))
    box = InboxStore(store.redis)
    rows = []
    for name, seat, task, area in (
        ("demo-eng-1", "eng-1@demo", "mine", "scripts/inbox"),
        ("demo-ci-1", "ci-1@demo", "peer", "scripts/inbox/store.py"),
        ("demo-eng-3", "eng-3@demo", "elsewhere", "scripts/doctor"),
        ("demo-ci-2", "ci-2@demo", "gone", "scripts/inbox"),
    ):
        store.seats.occupy(seat, name, 1)
        store.put_agent("demo", AgentRecord(name=name, lane=seat.split("-")[0], task=task, seat=seat, harness="codex"))
        rows.append({"id": task, "state": "claimed", "claimed_by": name, "territory": [area]})
    store.seats.record_exit("demo-ci-2", "ci-2@demo", "left")
    monkeypatch.setattr(cli, "connect", lambda: box)
    monkeypatch.setattr(cli, "registered_name", lambda: "")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "demo-eng-1")
    monkeypatch.setattr(LedgerClient, "tasks", lambda self, slug: rows)

    assert cli.main(["send", "ci-2@demo", "Coordinate the shared inbox work"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["warning"] == "seat ci-2@demo is empty; mail waits for its next occupant"
    assert result["overlapping_agents"] == [
        {"name": "demo-ci-1", "seat": "ci-1@demo", "task": "peer", "harness": "codex"}
    ]
    assert box.get(result["id"]).state == "pending"
    assert box.get(result["id"]).address == "ci-2@demo"
