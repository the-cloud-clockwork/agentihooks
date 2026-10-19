import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


def _environment(monkeypatch, slug="demo"):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(slug, "/r", 3, 2))
    box = InboxStore(store.redis)
    rows = []
    for name, seat, task, area in (
        ("demo-eng-1", f"eng-1@{slug}", "mine", "scripts/inbox"),
        ("demo-ci-1", f"ci-1@{slug}", "peer", "scripts/inbox/store.py"),
        ("demo-eng-3", f"eng-3@{slug}", "elsewhere", "scripts/doctor"),
        ("demo-ci-2", f"ci-2@{slug}", "gone", "scripts/inbox"),
    ):
        store.seats.occupy(seat, name, 1)
        store.put_agent(slug, AgentRecord(name=name, lane=seat.split("-")[0], task=task, seat=seat, harness="codex"))
        rows.append({"id": task, "state": "claimed", "claimed_by": name, "territory": [area]})
    store.seats.record_exit("demo-ci-2", f"ci-2@{slug}", "left")
    monkeypatch.setattr(cli, "connect", lambda: box)
    monkeypatch.setattr(cli, "registered_name", lambda: "")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "demo-eng-1")

    def tasks(self, requested):
        assert requested == slug
        return rows

    monkeypatch.setattr("scripts.inbox.recipient.LedgerClient.tasks", tasks)
    return store, box


@pytest.mark.parametrize("slug", ["demo", "demo@partition"])
def test_send_to_an_empty_seat_warns_and_names_only_live_overlapping_owners(monkeypatch, capsys, slug):
    store, box = _environment(monkeypatch, slug)
    assert cli.main(["send", f"ci-2@{slug}", "Coordinate the shared inbox work"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["warning"] == f"seat ci-2@{slug} is empty; mail waits for its next occupant"
    assert result["overlapping_agents"] == [
        {"name": "demo-ci-1", "seat": f"ci-1@{slug}", "task": "peer", "harness": "codex"}
    ]
    assert box.get(result["id"]).state == "pending"
    assert box.get(result["id"]).address == f"ci-2@{slug}"


def test_send_to_a_live_seat_reports_no_empty_seat_warning(monkeypatch, capsys):
    _environment(monkeypatch)
    assert cli.main(["send", "ci-1@demo", "Coordinate shared work"]) == 0
    assert "warning" not in json.loads(capsys.readouterr().out)


def test_a_sender_outside_the_swarm_gets_the_empty_seat_warning_with_no_claimed_owners(monkeypatch, capsys):
    _environment(monkeypatch)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "outside")
    assert cli.main(["send", "ci-2@demo", "Coordinate shared work"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["overlapping_agents"] == []
    assert result["warning"] == "seat ci-2@demo is empty; mail waits for its next occupant"


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_a_failed_task_read_keeps_mail_queued_and_reports_unavailable_owners(monkeypatch, capsys, failure):
    store, box = _environment(monkeypatch)

    def unavailable(self, slug):
        raise failure("unavailable")

    monkeypatch.setattr("scripts.inbox.recipient.LedgerClient.tasks", unavailable)
    assert cli.main(["send", "ci-2@demo", "Coordinate shared work"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["warning"] == "seat ci-2@demo is empty; mail waits for its next occupant"
    assert result["overlapping_agents"] == []
    assert result["overlap_error"] == "live task owners unavailable"
    assert box.get(result["id"]).state == "pending"


def test_a_finished_overlap_owner_is_excluded_even_when_its_seat_binding_remains(monkeypatch, capsys):
    store, box = _environment(monkeypatch)
    store.put_agent("demo", AgentRecord(name="demo-ci-1", lane="ci", task="peer", seat="ci-1@demo", state="finished"))
    assert cli.main(["send", "ci-2@demo", "Coordinate shared work"]) == 0
    assert json.loads(capsys.readouterr().out)["overlapping_agents"] == []
