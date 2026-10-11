import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.swarm.runtime import REMOTE_PANE, HerdrRuntime
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.herdr import target as target_module
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.reconcile import Record
from scripts.swarm_v2.herdr.target import (
    CALLER,
    DELIVERIES,
    KEPT,
    PENDING_LIMIT,
    Router,
    Target,
    client_runner,
    remote_environ,
)
from tests import sv2_hdr03_cases, sv2_hdr04_cases
from tests.sv2_hdr04_cases import Client, Fleet

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-HDR-04"
PANE = "w1:p1"
FX = sv2_hdr04_cases.fixture()
EXE_A, EXE_B = (endpoint.scope.execution_id for endpoint in sv2_hdr03_cases.endpoints())
CONVERSATION_A = FX["remote"][EXE_A]["conversation_id"]


def world(tmp_path):
    fleet, desired = Fleet(), sv2_hdr03_cases.endpoints()
    client = Client(fleet, tmp_path)
    client.register(desired)
    return client, fleet, desired


def resolved(client, endpoint, conversation=""):
    return client.router.resolve(endpoint.name, PANE, conversation)


def refusal(action) -> tuple[str, str]:
    with pytest.raises(EndpointRefused) as caught:
        action()
    return caught.value.reason, str(caught.value)


def test_two_servers_sharing_a_pane_id_each_receive_only_their_own_message(tmp_path):
    client, fleet, desired = world(tmp_path)
    for endpoint, text in zip(desired, ("to a", "to b"), strict=True):
        client.router.send(resolved(client, endpoint), "agent_prompt", f"op-{text}", text)
    assert fleet.received() == {"local": [], EXE_A: ["to a"], EXE_B: ["to b"]}


def test_a_command_names_the_saved_machine_profile_and_the_pane(tmp_path):
    client, _, desired = world(tmp_path)
    found = resolved(client, desired[0])
    argv = client.router.command(found, "pane_read", "--source", "visible")
    assert argv == ["herdr", "--machine", found.profile_id, "pane", "read", PANE, "--source", "visible"]
    assert found.session == "default" and found.incarnation == desired[0].incarnation


@pytest.mark.parametrize(
    "bare",
    [
        PANE,
        Target("", "default", f"{EXE_A}/2", PANE),
        Target("p", "", f"{EXE_A}/2", PANE),
        Target("p", "default", "", PANE),
    ],
)
def test_a_bare_remote_pane_is_refused_before_any_herdr_command(tmp_path, bare):
    client, fleet, _ = world(tmp_path)
    before = len(fleet.commands)
    reason, _ = refusal(lambda: client.router.send(bare, "agent_prompt", "op", "x"))
    assert reason == "bare_pane"
    assert len(fleet.commands) == before
    assert client.router.mismatches == {("bare", "bare_pane"): 1}


@pytest.mark.parametrize("pane", ["-w1:p1", "w1:p1 ", "w1", "p1", "w1:p1:p2", "wx:p1", ""])
def test_a_malformed_pane_id_is_refused(tmp_path, pane):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    bad = Target(found.profile_id, found.session, found.incarnation, pane)
    before = len(fleet.commands)
    assert refusal(lambda: client.router.command(bad, "pane_read"))[0] == "pane_id"
    assert len(fleet.commands) == before
    assert client.router.mismatches == {(found.profile_id, "pane_id"): 1}


def test_a_profile_the_controller_never_recorded_is_unregistered(tmp_path):
    client, _, desired = world(tmp_path)
    found = resolved(client, desired[0])
    manual = sv2_hdr03_cases.fixture()["manual"]["id"]
    forged = Target(manual, found.session, found.incarnation, PANE)
    assert refusal(lambda: client.router.command(forged, "pane_read"))[0] == "unregistered"


def test_a_recorded_profile_missing_from_the_client_is_unregistered(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    fleet.machines = [machine for machine in fleet.machines if machine["id"] != found.profile_id]
    assert refusal(lambda: client.router.command(found, "pane_read"))[0] == "unregistered"


@pytest.mark.parametrize(
    "change", [{"enabled": False}, {"label": "renamed"}, {"target": "ssh://elsewhere"}, {"session": "other"}]
)
def test_a_saved_machine_that_drifted_from_its_record_is_unregistered(tmp_path, change):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    next(machine for machine in fleet.machines if machine["id"] == found.profile_id).update(change)
    assert refusal(lambda: client.router.command(found, "pane_read"))[0] == "unregistered"


def test_a_target_naming_another_session_is_refused(tmp_path):
    client, _, desired = world(tmp_path)
    found = resolved(client, desired[0])
    moved = Target(found.profile_id, "other", found.incarnation, PANE)
    assert refusal(lambda: client.router.command(moved, "pane_read"))[0] == "session_mismatch"


def test_a_stale_server_incarnation_is_refused_before_any_pane_command(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    client.register(sv2_hdr03_cases.endpoints(desired[0].scope.generation + 1))
    before = len(fleet.commands)
    reason, message = refusal(lambda: client.router.send(found, "agent_prompt", "op", "x"))
    assert reason == "stale_incarnation"
    assert desired[0].incarnation in message
    assert fleet.commands[before:] == [["herdr", "machine", "list", "--json"]]


def test_an_incompatible_server_is_refused_and_counted(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    fleet.servers[found.profile_id] = sv2_hdr03_cases.sv2_hdr01_cases.fixture()["missing_forwarding_server"]
    assert refusal(lambda: client.router.command(found, "pane_read"))[0] == "incompatible"
    assert client.router.herdr_target_mismatch_total() == 1


@pytest.mark.parametrize("operation", ["pane_list", "workspace_create", "agent_list", "update", "nonsense"])
def test_an_operation_that_addresses_no_pane_is_refused(tmp_path, operation):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    before = len(fleet.commands)
    assert refusal(lambda: client.router.command(found, operation))[0] == "operation"
    assert len(fleet.commands) == before


@pytest.mark.parametrize("operation", ["pane_read", "agent_get", "pane_wait_output"])
def test_send_refuses_a_read_operation(tmp_path, operation):
    client, _, desired = world(tmp_path)
    assert refusal(lambda: client.router.send(resolved(client, desired[0]), operation, "op"))[0] == "operation"


def test_a_label_resolves_only_through_the_controller_record(tmp_path):
    client, _, desired = world(tmp_path)
    records = client.registrations.store.load()
    found = resolved(client, desired[1], "conversation")
    assert found == Target(records[EXE_B].profile_id, "default", desired[1].incarnation, PANE, "conversation")
    manual = sv2_hdr03_cases.fixture()["manual"]
    reason, message = refusal(lambda: client.router.resolve(manual["label"], PANE))
    assert reason == "unregistered"
    assert manual["id"] not in message and manual["target"] not in message
    assert refusal(lambda: client.router.resolve("unknown", PANE))[0] == "unregistered"


def test_the_agent_label_shared_by_every_server_resolves_nothing(tmp_path):
    client, fleet, _ = world(tmp_path)
    before = list(fleet.commands)
    assert refusal(lambda: client.router.resolve(FX["agent"], PANE))[0] == "unregistered"
    assert fleet.commands == before


def test_a_label_two_records_share_is_refused(tmp_path):
    client, _, desired = world(tmp_path)
    store = client.registrations.store
    records = store.load()
    records[EXE_B] = Record(**{**records[EXE_B].__dict__, "label": desired[0].name})
    store.save(records)
    assert refusal(lambda: resolved(client, desired[0]))[0] == "unregistered"


def test_resolve_refuses_a_record_whose_add_is_unconfirmed(tmp_path):
    client, fleet, desired = world(tmp_path)
    store = client.registrations.store
    records = store.load()
    records[EXE_A] = Record(**{**records[EXE_A].__dict__, "profile_id": None})
    store.save(records)
    assert refusal(lambda: resolved(client, desired[0]))[0] == "unregistered"


def test_resolve_refuses_a_malformed_pane(tmp_path):
    client, _, desired = world(tmp_path)
    assert refusal(lambda: client.router.resolve(desired[0].name, "-x"))[0] == "pane_id"


def test_a_pane_holding_another_conversation_receives_nothing(tmp_path):
    client, fleet, desired = world(tmp_path)
    wrong = resolved(client, desired[0], FX["local"]["conversation_id"])
    reason, _ = refusal(lambda: client.router.send(wrong, "agent_prompt", "op", "x"))
    assert reason == "conversation_mismatch"
    assert fleet.mutations() == [] and fleet.received()[EXE_A] == []
    assert not (tmp_path / DELIVERIES).exists()


def test_the_expected_conversation_admits_the_prompt(tmp_path):
    client, fleet, desired = world(tmp_path)
    client.router.send(resolved(client, desired[0], CONVERSATION_A), "agent_prompt", "op", "x")
    assert fleet.received()[EXE_A] == ["x"]
    gets = [command for command in fleet.commands if command[3:5] == ["agent", "get"]]
    assert gets == [["herdr", "--machine", resolved(client, desired[0]).profile_id, "agent", "get", PANE]]


def test_a_pane_that_reports_no_conversation_is_unconfirmed(tmp_path):
    client, fleet, desired = world(tmp_path)
    fleet.remote[EXE_A]["panes"][PANE].pop("agent_session")
    found = resolved(client, desired[0], CONVERSATION_A)
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op", "x"))[0] == "conversation_unconfirmed"
    assert fleet.received()[EXE_A] == []


@pytest.mark.parametrize("session", [{"kind": "path", "value": "x"}, {"kind": "id", "value": ""}, "id"])
def test_a_session_that_is_not_an_id_is_unconfirmed(tmp_path, session):
    client, fleet, desired = world(tmp_path)
    fleet.remote[EXE_A]["panes"][PANE]["agent_session"] = session
    found = resolved(client, desired[0], CONVERSATION_A)
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op", "x"))[0] == "conversation_unconfirmed"
    assert fleet.received()[EXE_A] == []


def test_a_target_without_an_expected_conversation_is_sent_on_its_pane_alone(tmp_path):
    client, fleet, desired = world(tmp_path)
    fleet.remote[EXE_A]["panes"][PANE].pop("agent_session")
    client.router.send(resolved(client, desired[0]), "agent_prompt", "op", "x")
    assert fleet.received()[EXE_A] == ["x"]
    assert not any(command[3:5] == ["agent", "get"] for command in fleet.commands)


def test_an_agent_reply_for_another_pane_is_refused(tmp_path):
    client, fleet, desired = world(tmp_path)
    fleet.remote[EXE_A]["panes"][PANE]["pane_id"] = "w2:p1"
    found = resolved(client, desired[0], CONVERSATION_A)
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op", "x"))[0] == "conversation_mismatch"
    assert fleet.received()[EXE_A] == []


@pytest.mark.parametrize("reply", [subprocess.CompletedProcess([], 1, "", "gone"), "not json", "[]", '{"result": 3}'])
def test_an_unconfirmable_conversation_is_refused(tmp_path, reply):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0], CONVERSATION_A)
    real = fleet.__call__

    def answer(command):
        if command[3:5] != ["agent", "get"]:
            return real(command)
        if isinstance(reply, subprocess.CompletedProcess):
            return reply
        return subprocess.CompletedProcess(command, 0, reply, "")

    client.router.run = answer
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op", "x"))[0] == "conversation_unconfirmed"
    assert fleet.received()[EXE_A] == []


def test_an_agent_get_that_never_answers_is_unconfirmed(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0], CONVERSATION_A)

    def hang(command):
        raise subprocess.TimeoutExpired(command, 30)

    client.router.run = hang
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op", "x"))[0] == "conversation_unconfirmed"


def test_a_repeated_operation_id_sends_nothing_again(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    first = client.router.send(found, "pane_send_text", "op-1", "hello")
    assert first.returncode == 0
    assert client.router.send(found, "pane_send_text", "op-1", "hello") is None
    assert fleet.received()[EXE_A] == ["hello"]


def test_an_operation_id_reused_for_another_pane_is_refused(tmp_path):
    client, fleet, desired = world(tmp_path)
    client.router.send(resolved(client, desired[0]), "agent_prompt", "op-1", "a")
    other = resolved(client, desired[1])
    reason, _ = refusal(lambda: client.router.send(other, "agent_prompt", "op-1", "b"))
    assert reason == "operation_reused"
    assert fleet.received()[EXE_B] == []
    assert client.router.mismatches == {(other.profile_id, "operation_reused"): 1}


@pytest.mark.parametrize("operation_id", ["", 7, None])
def test_a_send_needs_an_operation_id(tmp_path, operation_id):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    before = list(fleet.commands)
    assert refusal(lambda: client.router.send(found, "agent_prompt", operation_id, "x"))[0] == "operation_id"
    assert fleet.commands == before


def test_a_send_herdr_refuses_stays_pending_and_needs_a_new_operation_id(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    real = fleet.__call__
    client.router.run = lambda command: (
        subprocess.CompletedProcess(command, 1, "", "busy") if command[3:5] == ["agent", "prompt"] else real(command)
    )
    reason, message = refusal(lambda: client.router.send(found, "agent_prompt", "op-1", "x"))
    assert (reason, message) == ("send_refused", f"herdr exited 1 for op-1 on {found.incarnation}/{PANE}")
    entries = json.loads((tmp_path / DELIVERIES).read_text())["deliveries"]
    assert entries == {"op-1": {"target": f"{found.incarnation}/{PANE}", "state": "pending"}}
    client.router.run = fleet
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op-1", "x"))[0] == "ambiguous_delivery"
    client.router.send(found, "agent_prompt", "op-2", "x")
    assert fleet.received()[EXE_A] == ["x"]


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["herdr"], 30), OSError("broken pipe")])
def test_a_send_with_an_unknown_outcome_stays_pending_and_is_never_resent(tmp_path, error):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    real = fleet.__call__

    def lose(command):
        if command[3:5] == ["agent", "prompt"]:
            fleet.commands.append(list(command))
            raise error
        return real(command)

    client.router.run = lose
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op-1", "x"))[0] == "ambiguous_delivery"
    client.router.run = fleet
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op-1", "x"))[0] == "ambiguous_delivery"
    entries = json.loads((tmp_path / DELIVERIES).read_text())["deliveries"]
    assert entries == {"op-1": {"target": f"{found.incarnation}/{PANE}", "state": "pending"}}
    assert len(fleet.mutations()) == 1


def test_the_journal_keeps_only_the_newest_deliveries(tmp_path, monkeypatch):
    monkeypatch.setattr(target_module, "KEPT", 2)
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    real = fleet.__call__

    def lose(command):
        if command[-1] == "lost":
            raise subprocess.TimeoutExpired(command, 30)
        return real(command)

    client.router.run = lose
    refusal(lambda: client.router.send(found, "agent_prompt", "op-pending", "lost"))
    for number in range(3):
        client.router.send(found, "agent_prompt", f"op-{number}", "x")
    entries = json.loads((tmp_path / DELIVERIES).read_text())["deliveries"]
    assert list(entries) == ["op-pending", "op-1", "op-2"]
    assert entries["op-pending"]["state"] == "pending"
    assert KEPT == 1024


def test_a_journal_full_of_unknown_outcomes_refuses_new_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(target_module, "PENDING_LIMIT", 2)
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    real = fleet.__call__

    def lose(command):
        if command[3:5] == ["agent", "prompt"]:
            raise subprocess.TimeoutExpired(command, 30)
        return real(command)

    client.router.run = lose
    for number in range(2):
        refusal(lambda number=number: client.router.send(found, "agent_prompt", f"op-{number}", "x"))
    client.router.run = fleet
    before = list(fleet.commands)
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op-new", "x"))[0] == "journal_full"
    assert fleet.commands == before
    assert sorted(json.loads((tmp_path / DELIVERIES).read_text())["deliveries"]) == ["op-0", "op-1"]
    assert PENDING_LIMIT == 64


def test_the_operator_audit_settles_an_unknown_outcome(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    real = fleet.__call__
    client.router.run = lambda command: (
        subprocess.CompletedProcess(command, 1, "", "") if command[3:5] == ["agent", "prompt"] else real(command)
    )
    for operation_id in ("op-delivered", "op-lost"):
        refusal(lambda operation_id=operation_id: client.router.send(found, "agent_prompt", operation_id, "x"))
    client.router.run = fleet
    before = list(fleet.commands)
    client.router.settle("op-delivered", delivered=True)
    client.router.settle("op-lost", delivered=False)
    assert fleet.commands == before
    entries = json.loads((tmp_path / DELIVERIES).read_text())["deliveries"]
    assert entries == {"op-delivered": {"target": f"{found.incarnation}/{PANE}", "state": "sent"}}
    assert client.router.send(found, "agent_prompt", "op-delivered", "x") is None
    client.router.send(found, "agent_prompt", "op-lost", "again")
    assert fleet.received()[EXE_A] == ["again"]
    for operation_id in ("op-delivered", "op-unknown"):
        reason = refusal(lambda operation_id=operation_id: client.router.settle(operation_id, delivered=False))[0]
        assert reason == "not_pending"


def _drop_machine(fleet, found):
    fleet.machines = [machine for machine in fleet.machines if machine["id"] != found.profile_id]


def _lose_prompts(client, fleet):
    real = fleet.__call__

    def lose(command):
        if command[3:5] == ["agent", "prompt"]:
            raise subprocess.TimeoutExpired(command, 30)
        return real(command)

    client.router.run = lose


REFUSALS = {
    "bare_command": (
        lambda c, f, d, t: c.router.command(Target("", "default", t.incarnation, PANE), "pane_read"),
        lambda d, t: ("bare_pane", target_module.BARE, "bare"),
    ),
    "bare_refresh": (
        lambda c, f, d, t: c.router.refresh(Target(t.profile_id, "", t.incarnation, PANE)),
        lambda d, t: ("bare_pane", target_module.BARE, "bare"),
    ),
    "bare_send": (
        lambda c, f, d, t: c.router.send(PANE, "agent_prompt", "op", "x"),
        lambda d, t: ("bare_pane", target_module.BARE, "bare"),
    ),
    "command_operation": (
        lambda c, f, d, t: c.router.command(t, "pane_list"),
        lambda d, t: ("operation", "pane_list does not address one pane", t.profile_id),
    ),
    "send_operation": (
        lambda c, f, d, t: c.router.send(t, "pane_read", "op"),
        lambda d, t: ("operation", "pane_read is not a pane mutation", t.profile_id),
    ),
    "session_mismatch": (
        lambda c, f, d, t: c.router.command(Target(t.profile_id, "other", t.incarnation, PANE), "pane_read"),
        lambda d, t: ("session_mismatch", f"{d[0].name} is saved for another remote session", t.profile_id),
    ),
    "foreign_profile": (
        lambda c, f, d, t: c.router.command(Target("foreign", "default", t.incarnation, PANE), "pane_read"),
        lambda d, t: ("unregistered", "machine foreign is not registered by this controller", "foreign"),
    ),
    "machine_missing": (
        lambda c, f, d, t: (_drop_machine(f, t), c.router.command(t, "pane_read")),
        lambda d, t: ("unregistered", f"{d[0].name} is not saved on this herdr client", t.profile_id),
    ),
    "unknown_label": (
        lambda c, f, d, t: c.router.resolve("unknown", PANE),
        lambda d, t: ("unregistered", "unknown is not one machine this controller registered", "bare"),
    ),
    "resolve_pane": (
        lambda c, f, d, t: c.router.resolve(d[0].name, "w1"),
        lambda d, t: ("pane_id", "a herdr pane id reads w<number>:p<number>", t.profile_id),
    ),
    "refresh_retired": (
        lambda c, f, d, t: (c.register(d[1:]), c.router.refresh(t)),
        lambda d, t: ("unregistered", f"{EXE_A} has no registered terminal", t.profile_id),
    ),
    "refresh_malformed_incarnation": (
        lambda c, f, d, t: c.router.refresh(Target(t.profile_id, "default", f"{EXE_A}/extra/2", PANE)),
        lambda d, t: ("unregistered", f"{EXE_A}/extra has no registered terminal", t.profile_id),
    ),
    "refresh_stale": (
        lambda c, f, d, t: (c.register(sv2_hdr03_cases.endpoints(3)), c.router.refresh(t)),
        lambda d, t: ("stale_incarnation", f"{t.incarnation} was replaced", t.profile_id),
    ),
    "command_stale": (
        lambda c, f, d, t: (c.register(sv2_hdr03_cases.endpoints(3)), c.router.command(t, "pane_read")),
        lambda d, t: ("stale_incarnation", f"{t.incarnation} was replaced", t.profile_id),
    ),
    "unconfirmed": (
        lambda c, f, d, t: (
            f.remote[EXE_A]["panes"][PANE].pop("agent_session"),
            c.router.send(replace(t, conversation_id=CONVERSATION_A), "agent_prompt", "op", "x"),
        ),
        lambda d, t: ("conversation_unconfirmed", f"{PANE} reported no conversation", t.profile_id),
    ),
    "mismatch": (
        lambda c, f, d, t: c.router.send(replace(t, conversation_id="other"), "agent_prompt", "op", "x"),
        lambda d, t: ("conversation_mismatch", f"{PANE} holds another conversation", t.profile_id),
    ),
    "reused": (
        lambda c, f, d, t: (
            c.router.send(t, "agent_prompt", "op-1", "x"),
            c.router.send(replace(t, pane_id="w1:p2"), "agent_prompt", "op-1", "x"),
        ),
        lambda d, t: ("operation_reused", "op-1 was sent to another pane", t.profile_id),
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_refusal_names_its_reason_message_and_counted_profile(tmp_path, case):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    action, expected = REFUSALS[case]
    reason, message, profile_id = expected(desired, found)
    assert refusal(lambda: action(client, fleet, desired, found)) == (reason, message)
    assert client.router.mismatches == {(profile_id, reason): 1}


UNCOUNTED = {
    "read_only": (
        lambda c, f, t: Router(
            c.registrations.store, c.router.catalog, c.qualifier, f, c.root / "r", mutations=False
        ).send(t, "agent_prompt", "op", "x"),
        "pane mutations are switched off; only read only inspection runs",
    ),
    "operation_id": (
        lambda c, f, t: c.router.send(t, "agent_prompt", "", "x"),
        "a pane mutation needs an operation id",
    ),
    "journal_unreadable": (
        lambda c, f, t: ((c.root / DELIVERIES).write_text("{"), c.router.send(t, "agent_prompt", "op", "x")),
        "the pane delivery journal is unreadable",
    ),
    "not_pending": (
        lambda c, f, t: c.router.settle("op-unknown", delivered=True),
        "op-unknown has no unknown outcome to settle",
    ),
}


@pytest.mark.parametrize("case", sorted(UNCOUNTED))
def test_a_refusal_that_names_no_target_carries_its_exact_message(tmp_path, case):
    client, fleet, desired = world(tmp_path)
    action, message = UNCOUNTED[case]
    assert refusal(lambda: action(client, fleet, resolved(client, desired[0]))) == (case, message)
    assert client.router.mismatches == {}


def test_a_full_journal_names_the_audit_it_waits_on(tmp_path, monkeypatch):
    monkeypatch.setattr(target_module, "PENDING_LIMIT", 1)
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    _lose_prompts(client, fleet)
    refusal(lambda: client.router.send(found, "agent_prompt", "op-0", "x"))
    expected = ("journal_full", "too many pane sends await an audit of their unknown outcome")
    assert refusal(lambda: client.router.send(found, "agent_prompt", "op-1", "x")) == expected


def test_sent_deliveries_never_count_toward_the_pending_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(target_module, "PENDING_LIMIT", 2)
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    for number in range(3):
        client.router.send(found, "agent_prompt", f"op-{number}", f"m{number}")
    assert fleet.received()[EXE_A] == ["m0", "m1", "m2"]


def test_an_incompatible_server_is_refused_with_the_qualifier_reasons(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0])
    fleet.servers[found.profile_id] = sv2_hdr03_cases.sv2_hdr01_cases.fixture()["missing_forwarding_server"]
    reason, message = refusal(lambda: client.router.command(found, "pane_read"))
    assert reason == "incompatible" and message.startswith("herdr server lacks")
    assert client.router.mismatches == {(found.profile_id, "incompatible"): 1}


def test_refresh_takes_the_session_saved_now(tmp_path):
    client, _, desired = world(tmp_path)
    found = resolved(client, desired[0])
    assert client.router.refresh(replace(found, session="old")) == found


def test_resolve_expects_no_conversation_unless_one_is_named(tmp_path):
    client, _, desired = world(tmp_path)
    assert client.router.resolve(desired[0].name, PANE).conversation_id == ""


@pytest.mark.parametrize(
    "text", ["{", "[]", '{"version": 2, "deliveries": {}}', '{"version": 1}', '{"version": 1, "deliveries": []}']
)
def test_an_unreadable_journal_refuses_every_send(tmp_path, text):
    client, fleet, desired = world(tmp_path)
    (tmp_path / DELIVERIES).write_text(text)
    reason = refusal(lambda: client.router.send(resolved(client, desired[0]), "agent_prompt", "op", "x"))[0]
    assert reason == "journal_unreadable"
    assert fleet.mutations() == []


def test_the_journal_is_private_and_versioned(tmp_path):
    client, _, desired = world(tmp_path)
    client.router.send(resolved(client, desired[0]), "agent_prompt", "op", "x")
    path = tmp_path / DELIVERIES
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["version"] == 1
    assert not list(tmp_path.glob(f".{DELIVERIES}.tmp"))


def test_refresh_follows_a_reregistered_profile_for_the_same_incarnation(tmp_path):
    client, fleet, desired = world(tmp_path)
    found = resolved(client, desired[0], CONVERSATION_A)
    fleet.machines = [machine for machine in fleet.machines if machine["id"] != found.profile_id]
    client.register(desired)
    before = len(fleet.commands)
    fresh = client.router.refresh(found)
    assert fresh.profile_id != found.profile_id
    assert (fresh.session, fresh.incarnation, fresh.pane_id, fresh.conversation_id) == (
        "default",
        found.incarnation,
        PANE,
        CONVERSATION_A,
    )
    assert not fleet.mutations() and all(command[3:5] != ["agent", "prompt"] for command in fleet.commands[before:])


def test_refresh_refuses_a_newer_generation_and_a_retired_attempt(tmp_path):
    client, _, desired = world(tmp_path)
    found = resolved(client, desired[0])
    client.register(sv2_hdr03_cases.endpoints(desired[0].scope.generation + 1))
    assert refusal(lambda: client.router.refresh(found))[0] == "stale_incarnation"
    client.register(desired[1:])
    assert refusal(lambda: client.router.refresh(found))[0] == "unregistered"


def test_refresh_refuses_a_bare_target(tmp_path):
    client, _, _ = world(tmp_path)
    assert refusal(lambda: client.router.refresh(Target("", "", "", PANE)))[0] == "bare_pane"


def test_remote_commands_drop_the_local_caller_pane_identity():
    assert remote_environ(FX["caller_environment"]) == {"PATH": "/usr/bin:/bin"}
    assert set(CALLER) == {"HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_SOCKET_PATH"}


def test_the_client_runner_runs_herdr_without_the_caller_pane(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    client_runner(FX["caller_environment"])(["herdr", "--machine", "p", "agent", "get", PANE])
    assert seen["env"] == {"PATH": "/usr/bin:/bin"}
    assert seen["command"] == ["herdr", "--machine", "p", "agent", "get", PANE]


def test_the_router_runs_through_its_injected_runner(tmp_path):
    client, fleet, _ = world(tmp_path)
    router = Router(client.registrations.store, client.router.catalog, client.qualifier, fleet, tmp_path / DELIVERIES)
    assert router.run is fleet and router.herdr_target_mismatch_total() == 0


def test_rollback_keeps_read_only_inspection_and_refuses_every_pane_mutation(tmp_path):
    client, fleet, desired = world(tmp_path)
    store, catalog = client.registrations.store, client.router.catalog
    router = Router(store, catalog, client.qualifier, fleet, tmp_path / DELIVERIES, mutations=False)
    found = router.resolve(desired[0].name, PANE)
    before = len(fleet.commands)
    assert refusal(lambda: router.send(found, "agent_prompt", "op", "x"))[0] == "read_only"
    assert len(fleet.commands) == before and not (tmp_path / DELIVERIES).exists()
    argv = router.command(found, "pane_read")
    assert fleet(argv).returncode == 0
    assert fleet.mutations() == [] and fleet.received()[EXE_A] == []


def remote_agent(**changes):
    fields = {"runtime_backend": "kubernetes", "pane_id": PANE, "execution_id": EXE_A, "generation": 2}
    return AgentRecord(FX["agent"], "eng", "t1", **(fields | changes))


def local_runtime(tmp_path):
    calls = []

    def herdr(args):
        calls.append(args)
        return {"agent": {"pane_id": PANE, "name": FX["agent"], "agent_status": "working"}, "text": ""}

    runtime = HerdrRuntime(home=tmp_path, herdr=herdr, choose=lambda *_: ("claude", "open"))
    runtime.end = pytest.fail
    return runtime, calls


def test_the_local_runtime_never_reaches_a_remote_agent_through_the_local_server(tmp_path):
    runtime, calls = local_runtime(tmp_path)
    agent = remote_agent()
    assert runtime.observe(agent).state == "unknown"
    assert runtime.status(agent) == "unknown"
    runtime.nudge(agent, "wake")
    assert runtime.name_pane(agent) is False
    assert runtime.retire_process(agent, 4242) is False
    assert runtime.refusal(agent) == {"process": 0, "refusal": REMOTE_PANE}
    assert calls == []


def test_the_local_runtime_still_reaches_a_local_agent_with_the_same_pane(tmp_path):
    runtime, calls = local_runtime(tmp_path)
    agent = remote_agent(runtime_backend="local", execution_id="", generation=0)
    assert runtime.observe(agent).state == "working"
    runtime.nudge(agent, "wake")
    assert [call[:2] for call in calls] == [["agent", "get"], ["pane", "read"], ["agent", "prompt"]]


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_hdr04_cases.run_case(case), sv2_hdr04_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
