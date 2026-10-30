import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.unit


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch) -> InboxStore:
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    store.names.adopt("crew", "123456", "crew", "repo")
    sender = store.names.next("crew", "eng")
    master = store.names.next("crew", "master")
    store.seats.occupy("eng-1@crew", sender, 1)
    store.seats.occupy("master@crew", master, 1)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", sender)
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions",
        lambda **kwargs: {"sender-session": {"name": sender}, "master-session": {"name": master}},
    )
    return store


def test_unknown_destination_is_refused_with_sender_swarm_addresses_and_nothing_stored(
    inbox: InboxStore, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(inbox.redis.keys())
    assert cli.main(["send", "swarm", "status"]) == 1
    error = capsys.readouterr().err
    assert "unknown inbox address swarm" in error
    for address in ("eng-1@crew", "master@crew"):
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


@pytest.mark.parametrize("command", ["send", "reply"])
def test_unknown_address_refusal_is_bounded_and_lists_only_live_seats(
    inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    historical = [inbox.names.next("crew", "eng") for _ in range(2000)]
    inbox.seats.occupy("eng-2@crew", historical[-2], 1)
    inbox.seats.occupy("eng-2@crew", historical[-1], 2)
    active = {
        "master-session": {"name": "master@123456-0001"},
        "old-session": {"name": historical[-2]},
        "unseated-session": {"name": "unseated-agent"},
    }
    inbox.seats.occupy("master@other", "outsider", 1)
    active["outsider-session"] = {"name": "outsider"}
    for index in range(20):
        name = inbox.names.next("crew", "ci")
        inbox.seats.occupy(f"ci-{index + 1}@crew", name, 1)
        active[f"session-{index}"] = {"name": name}
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: active)
    args = ["send", "absent-recipient", "hello"]
    if command == "reply":
        item = inbox.send("absent-recipient", "engineer@123456-0001", "question")
        args = ["reply", item.id, "answer"]
    assert cli.main(args) == 1
    error = capsys.readouterr().err
    assert "unknown inbox address absent-recipient" in error
    assert len(error) < 500
    suggestions = error.strip().split("live seats: ", 1)[1].split(", ")
    assert suggestions == sorted([f"ci-{index + 1}@crew" for index in range(20)] + ["master@crew"])[:8]
    assert all(seat.startswith("ci-") and seat.endswith("@crew") or seat == "master@crew" for seat in suggestions)
    assert "eng-2@crew" not in error
    assert not any(name in error for name in historical)


def test_unknown_address_without_live_seats_has_no_historical_suggestions(
    inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    assert cli.main(["send", "absent-recipient", "hello"]) == 1
    assert (
        capsys.readouterr().err.strip() == "agentihooks msg: unknown inbox address absent-recipient; live seats: none"
    )


def test_unseated_sender_gets_live_seats_across_swarms(
    inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "standalone")
    inbox.seats.occupy("master@other", "outsider", 1)
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions",
        lambda **kwargs: {"one": {"name": "master@123456-0001"}, "two": {"name": "outsider"}, "empty": {}},
    )
    assert cli.main(["send", "absent-recipient", "hello"]) == 1
    assert capsys.readouterr().err.strip() == (
        "agentihooks msg: unknown inbox address absent-recipient; live seats: master@crew, master@other"
    )
