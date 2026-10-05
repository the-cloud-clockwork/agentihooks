import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def inbox(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    store.names.adopt("crew", "123456", "crew", "repo")
    sender = store.names.next("crew", "eng")
    master = store.names.next("crew", "master")
    store.seats.occupy("eng-1@crew", sender, 1)
    store.seats.occupy("master@crew", master, 1)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", sender)
    return store


def test_unknown_destination_is_refused_with_sender_swarm_addresses_and_nothing_stored(inbox, capsys):
    before = sorted(inbox.redis.keys())
    assert cli.main(["send", "swarm", "status"]) == 1
    error = capsys.readouterr().err
    assert "unknown inbox address swarm" in error
    for address in ("engineer@123456-0001", "master@123456-0001", "eng-1@crew", "master@crew", "operator"):
        assert address in error
    assert sorted(inbox.redis.keys()) == before


def test_reply_to_unknown_sender_is_refused_without_closing_or_storing(inbox, capsys):
    sender = "engineer@123456-0001"
    item = inbox.send("missing-sender", sender, "question")
    before = sorted(inbox.redis.keys())
    assert cli.main(["reply", item.id, "answer"]) == 1
    assert "unknown inbox address missing-sender" in capsys.readouterr().err
    assert inbox.get(item.id).state == "pending"
    assert sorted(inbox.redis.keys()) == before


@pytest.mark.parametrize(
    "address", ["master@123456-0001", "master@crew", "eng-2@crew", "operator", "bob", "session-bob", "old-master"]
)
def test_real_names_seats_operator_and_session_ids_still_accept_messages(inbox, monkeypatch, capsys, address):
    from scripts.inbox import addresses

    inbox.seats.occupy("eng-2@crew", "", 1)
    inbox.names.alias("old-master", "master@123456-0001")
    monkeypatch.setattr(addresses, "get_active_sessions", lambda **kwargs: {"session-bob": {"name": "bob"}})
    assert cli.main(["send", address, "hello"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["to"] == ("master@123456-0001" if address == "old-master" else address)
    assert inbox.get(result["id"]).state == "pending"


@pytest.mark.parametrize("address", ["eng-99@crew", "master@missing", "engineer@123456-9999"])
def test_an_address_shape_alone_is_not_registration(inbox, capsys, address):
    before = sorted(inbox.redis.keys())
    assert cli.main(["send", address, "hello"]) == 1
    assert "unknown inbox address" in capsys.readouterr().err
    assert sorted(inbox.redis.keys()) == before


def test_suggestions_include_only_the_senders_swarm(inbox, capsys):
    inbox.names.adopt("other", "654321", "other", "repo")
    inbox.names.next("other", "eng")
    inbox.seats.occupy("master@other", "outsider", 1)
    assert cli.main(["send", "swarm", "hello"]) == 1
    error = capsys.readouterr().err
    assert "master@crew" in error
    assert "master@other" not in error
    assert "engineer@654321-0001" not in error


def test_reply_to_a_registered_sender_still_closes_the_original(inbox, capsys):
    item = inbox.send("master@123456-0001", "engineer@123456-0001", "question")
    assert cli.main(["reply", item.id, "answer"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["to"] == "master@123456-0001"
    assert inbox.get(item.id).state == "done"
