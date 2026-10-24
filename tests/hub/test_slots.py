import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hive import account_slots
from scripts.hub import slots
from tests.hive.test_account_slots import reading


@pytest.fixture
def hub():
    now = [1000]
    bus = open_bus(BusConfig(scoped=True), clock=lambda: now[0])
    slots.serve(bus, clock=lambda: now[0])
    yield bus, now
    bus.close()


def member(bus, swarm, hive, report=None):
    return account_slots.Remote(
        account_slots.asker(bus.for_swarm(swarm), swarm, hive), hive, lambda: report or reading()
    )


def holders(bus):
    entry = bus.state(slots.BUCKETS[0]).get(account_slots.key("claude", "a"))
    return json.loads(entry.value)["holders"]


def test_the_hub_takes_renews_and_returns_a_member_slot_over_its_swarm_subject(hub):
    bus, now = hub
    first, second = member(bus, "rig", "one"), member(bus, "rig", "two")
    assert first.take("execution-one", "claude") == "a"
    assert holders(bus) == {"execution-one": {"hive": "one", "expires_ms": 1000 + account_slots.TTL_MS}}
    now[0] += account_slots.TTL_MS - 1
    first.heartbeat(reading(), {"execution-one": {"pane": {"state": "working"}}})
    now[0] += 2
    assert second.take("execution-two", "claude") is None
    first.release("execution-one", "claude", "a")
    assert second.take("execution-two", "claude") == "a"


def test_members_of_different_swarms_share_one_account_total(hub):
    bus, _ = hub
    assert member(bus, "rig", "one").take("execution-one", "claude") == "a"
    assert member(bus, "other", "two").take("execution-two", "claude") is None
    assert bus.for_swarm("rig").state(slots.BUCKETS[0]).get(account_slots.key("claude", "a")) is None


def test_a_member_launch_reserves_through_the_hub_and_returns_on_failure(hub):
    bus, _ = hub
    first, second = member(bus, "rig", "one"), member(bus, "other", "two")

    def launch(account):
        assert account == "a"
        assert second.take("other", "claude") is None
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError, match="^launch failed$"):
        first.launch("execution-one", "claude", "", launch)
    assert second.take("other", "claude") == "a"


@pytest.mark.parametrize(
    ("subject", "request_body", "error"),
    [
        ("slots.rig.one", {"op": "steal", "hive": "one"}, "unknown slot operation 'steal'"),
        ("slots.rig.one", {"op": "take"}, "slot request is missing 'hive'"),
        ("swarm.rig.slots", {"op": "take", "hive": "one"}, "not a slot request subject: 'swarm.rig.slots'"),
        ("slots.rig.one.extra", {"op": "take", "hive": "one"}, "not a slot request subject: 'slots.rig.one.extra'"),
    ],
)
def test_the_hub_refuses_malformed_slot_requests(hub, subject, request_body, error):
    bus, _ = hub
    answer = json.loads(slots.handle(bus, subject, json.dumps(request_body).encode()))
    assert answer == {"ok": False, "error": error}


def test_a_refused_request_raises_unavailable_on_the_member(hub):
    bus, _ = hub
    ask = account_slots.asker(bus.for_swarm("rig"), "rig", "one")
    with pytest.raises(account_slots.Unavailable, match="^unknown slot operation 'steal'$"):
        ask({"op": "steal", "hive": "one"})


def test_a_scoped_member_cannot_address_another_swarm_subject(hub):
    bus, _ = hub
    with pytest.raises(ValueError, match="^a scoped bus cannot request outside swarm 'rig'$"):
        account_slots.asker(bus.for_swarm("rig"), "other", "one")({"op": "take", "hive": "one"})


def test_a_member_asks_on_the_slot_subject_of_its_own_hive():
    from unittest.mock import Mock

    bus = Mock()
    bus.request.return_value = json.dumps({"ok": True, "result": "a"}).encode()
    assert account_slots.asker(bus, "rig", "one", 7)({"op": "take", "hive": "one"}) == "a"
    bus.request.assert_called_once_with("slots.rig.one", b'{"op": "take", "hive": "one"}', 7)


def test_a_hive_name_that_cannot_be_a_subject_token_is_unavailable():
    with pytest.raises(
        account_slots.Unavailable,
        match=r"^hive 'host\.example' cannot ask for account slots \(invalid event subject token 'host\.example'\)$",
    ):
        account_slots.asker(None, "rig", "host.example")


@pytest.mark.parametrize(
    "request_body",
    [
        {"op": "take", "hive": "two", "execution": "e", "harness": "claude", "report": reading()},
        {"op": "release", "hive": "two", "execution": "execution-one", "harness": "claude", "account": "a"},
        {"op": "heartbeat", "hive": "two", "report": reading(), "executions": {}},
    ],
)
def test_a_slot_request_naming_another_hive_than_its_credential_is_refused_and_logged(hub, capsys, request_body):
    bus, _ = hub
    assert member(bus, "rig", "one").take("execution-one", "claude") == "a"
    request = account_slots.request_subject("rig", "one")
    answer = json.loads(bus.for_swarm("rig").request(request, json.dumps(request_body).encode(), 1000))
    refusal = "slot request names hive 'two' but the sending credential is bound to hive 'one'"
    assert answer == {"ok": False, "error": refusal}
    assert capsys.readouterr().err == f"hub: refused a {request_body['op']} in swarm rig: {refusal}\n"
    assert holders(bus) == {"execution-one": {"hive": "one", "expires_ms": 1000 + account_slots.TTL_MS}}


def test_a_request_nobody_serves_times_out():
    bus = open_bus(BusConfig())
    try:
        with pytest.raises(TimeoutError, match="^no responders for 'slots.rig.one'$"):
            bus.request("slots.rig.one", b"{}", 10)
    finally:
        bus.close()


def test_serve_provisions_the_hub_owned_account_buckets():
    bus = open_bus(BusConfig())
    opened = []
    original = bus.state
    bus.state = lambda name: opened.append(name) or original(name)
    try:
        slots.serve(bus)
        assert opened == ["account-slots", "hive-slot-accounts"]
    finally:
        bus.close()


def test_serve_answers_in_one_queue_group_across_hub_replicas():
    from unittest.mock import ANY, Mock

    bus = Mock()
    slots.serve(bus)
    bus.serve.assert_called_once_with("slots.*.*", "hub-slots", ANY)


def test_a_take_without_a_reading_is_refused(hub):
    bus, _ = hub
    request = {"op": "take", "hive": "one", "execution": "e", "harness": "claude"}
    answer = json.loads(slots.handle(bus, "slots.rig.one", json.dumps(request).encode()))
    assert answer == {"ok": False, "error": "slot request is missing 'report'"}


def test_a_failure_without_a_message_is_named_by_its_type(hub, monkeypatch):
    from scripts.events.bus import StateConflict

    bus, _ = hub

    def conflict(*args):
        raise StateConflict

    monkeypatch.setattr(account_slots.Slots, "release", conflict)
    request = {"op": "release", "hive": "one", "execution": "e", "harness": "claude", "account": "a"}
    answer = json.loads(slots.handle(bus, "slots.rig.one", json.dumps(request).encode()))
    assert answer == {"ok": False, "error": "StateConflict"}
