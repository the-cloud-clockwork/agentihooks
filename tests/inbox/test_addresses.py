import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.unit


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
    routed = {"old-master": "master@123456-0001", "operator": "master@crew"}
    assert result["to"] == routed.get(address, address)
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


@pytest.mark.parametrize("seat", ["master@crew", "master-2@crew"])
@pytest.mark.parametrize("operation", ["send", "reply"])
def test_master_name_without_registry_entry_routes_to_occupied_seat(
    inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], seat: str, operation: str
) -> None:
    master = "master@123456-0099"
    worker = "engineer@123456-0001"
    inbox.seats.occupy(seat, master, 2)
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    assert not inbox.names.entry(master)
    original = inbox.send(master, worker, "question")
    args = ["reply", original.id, "answer"] if operation == "reply" else ["send", master, "answer"]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    answer = inbox.get(result["id"])
    assert result["to"] == seat
    assert (answer.sender, answer.address, answer.text) == (worker, seat, "answer")
    assert [item.id for item in inbox.inbox(seat)] == [answer.id]
    assert inbox.get(original.id).state == ("done" if operation == "reply" else "pending")


def test_replaced_master_without_registration_is_still_refused(
    inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    master = "master@123456-0099"
    inbox.seats.occupy("master@crew", master, 2)
    inbox.seats.occupy("master@crew", "master@123456-0100", 3)
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    assert cli.main(["send", master, "answer"]) == 1
    assert "unknown inbox address" in capsys.readouterr().err
    assert not inbox.inbox("master@crew")
