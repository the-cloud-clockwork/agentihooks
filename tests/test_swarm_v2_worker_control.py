import base64
import json
import sys

import pytest

from scripts.swarm_v2.runtime.operations import digest
from scripts.swarm_v2.worker import control

pytestmark = pytest.mark.unit

COMMAND_ID = "cmd-" + "a" * 32
EXECUTION = {"execution_id": "exe-" + "1" * 32, "generation": 4}
IDENTITY = {"command_id": COMMAND_ID, **EXECUTION}
NO_IDENTITY = {"command_id": "", "execution_id": "", "generation": 0}


def record(**changes):
    payload = changes.pop("payload", {"text": "$(id) `id` \"q\" 'q'\nnext ü"})
    kind = changes.pop("kind", "answer")
    return {
        "command_id": COMMAND_ID,
        **EXECUTION,
        "kind": kind,
        "payload": payload,
        "payload_digest": digest({"kind": kind, "payload": payload}),
        "expires_at_ms": 5000,
        **changes,
    }


def raw(envelope):
    return base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()


def refused(reason, ids=IDENTITY):
    return {**ids, "state": "refused", "reason": reason}


@pytest.fixture
def home(tmp_path):
    (tmp_path / "launch.json").write_text(json.dumps(EXECUTION))
    return tmp_path


def handle(home, mode, text, now_ms=4000):
    return control.handle(mode, text, home / "inbox", home / "state.json", home / "launch.json", now_ms)


def run_main(home, capsys, args, now_ms=4000):
    code = control.main(
        args, inbox=home / "inbox", state=home / "state.json", launch=home / "launch.json", clock=lambda: now_ms / 1000
    )
    return code, capsys.readouterr().out


def test_the_helper_paths_modes_and_envelope_fields_are_fixed():
    assert str(control.INBOX) == "/home/worker/commands/inbox"
    assert str(control.STATE) == "/home/worker/commands/state.json"
    assert str(control.LAUNCH) == "/var/run/swarm/launch/launch.json"
    assert control.MODES == ("status", "deliver")
    assert control.ENVELOPE == (
        "command_id",
        "execution_id",
        "generation",
        "kind",
        "payload",
        "payload_digest",
        "expires_at_ms",
    )
    assert control.USAGE == "usage: status ENVELOPE | deliver ENVELOPE"


def test_encode_keeps_only_the_envelope_fields_as_url_safe_canonical_json():
    envelope = record()
    text = control.encode({**envelope, "state": "issued", "issued_at_ms": 1})
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    assert text == base64.urlsafe_b64encode(canonical).decode()
    assert control.decode(text) == envelope


def test_decode_reads_url_safe_characters_and_padding():
    envelope = record(payload={"text": "\n>>>???~~~ü" * 5})
    text = control.encode(envelope)
    assert {"-", "_"} & set(text)
    assert control.decode(text) == envelope
    assert control.decode(raw(record())) == record()


@pytest.mark.parametrize(
    "text, message",
    [
        ("ab+/", "the envelope is not url safe base64"),
        ("YWJj!", "the envelope is not url safe base64"),
        ("é", "the envelope is not url safe base64"),
        ("YWJj\n", "the envelope is not url safe base64"),
        ("YWJj===", "the envelope is not url safe base64"),
        ("", "the envelope is not url safe base64"),
        (raw([1]), "the envelope fields are wrong"),
        (raw({k: v for k, v in record().items() if k != "kind"}), "the envelope fields are wrong"),
        (raw({**record(), "extra": 1}), "the envelope fields are wrong"),
        (raw(record(command_id="cmd-short")), "the command id is malformed"),
        (raw(record(command_id=7)), "the command id is malformed"),
        (raw(record(command_id="cmd-" + "a" * 32 + "\n")), "the command id is malformed"),
        (raw(record(generation=True)), "the generation and expiry must be integers"),
        (raw(record(generation="4")), "the generation and expiry must be integers"),
        (raw(record(expires_at_ms=5000.0)), "the generation and expiry must be integers"),
        (raw({**record(), "payload": {"text": "other"}}), "the payload digest does not match"),
        (raw({**record(), "kind": "drain"}), "the payload digest does not match"),
    ],
)
def test_decode_refuses_any_envelope_that_is_not_exactly_the_issued_command(text, message):
    with pytest.raises(ValueError) as caught:
        control.decode(text)
    assert str(caught.value) == message


@pytest.mark.parametrize("text", ["YWJ", "YWJjZA", "_w=="])
def test_decode_refuses_bad_padding_and_bytes_that_are_not_json(text):
    with pytest.raises(ValueError):
        control.decode(text)


def test_status_reads_the_inbox_before_the_worker_state(home):
    inbox, state = home / "inbox", home / "state.json"
    assert control.status(COMMAND_ID, inbox, state) == "absent"
    state.write_text(json.dumps({"cmd-" + "b" * 32: {}}))
    assert control.status(COMMAND_ID, inbox, state) == "absent"
    state.write_text(json.dumps({COMMAND_ID: {}}))
    assert control.status(COMMAND_ID, inbox, state) == "known"
    inbox.mkdir()
    (inbox / f"{COMMAND_ID}.json").write_text("{}")
    assert control.status(COMMAND_ID, inbox, state) == "queued"


def test_deliver_queues_the_envelope_once_and_leaves_no_staged_file(home):
    envelope = record()
    assert handle(home, "deliver", control.encode(envelope)) == (0, {**IDENTITY, "state": "queued"})
    assert json.loads((home / "inbox" / f"{COMMAND_ID}.json").read_bytes()) == envelope
    assert handle(home, "deliver", control.encode(envelope)) == (0, {**IDENTITY, "state": "known"})
    assert sorted(path.name for path in (home / "inbox").iterdir()) == [f"{COMMAND_ID}.json"]


def test_deliver_creates_a_missing_inbox_tree(tmp_path):
    (tmp_path / "launch.json").write_text(json.dumps(EXECUTION))
    inbox = tmp_path / "commands" / "inbox"
    state, launch = tmp_path / "state.json", tmp_path / "launch.json"
    assert control.handle("deliver", control.encode(record()), inbox, state, launch, 0) == (
        0,
        {**IDENTITY, "state": "queued"},
    )
    assert (inbox / f"{COMMAND_ID}.json").exists()


def test_status_answers_for_the_envelope_without_writing(home):
    envelope = control.encode(record())
    assert handle(home, "status", envelope) == (0, {**IDENTITY, "state": "absent"})
    assert not (home / "inbox").exists()
    handle(home, "deliver", envelope)
    assert handle(home, "status", envelope, now_ms=9000) == (0, {**IDENTITY, "state": "queued"})


def test_deliver_answers_known_when_the_worker_already_holds_the_command(home):
    (home / "state.json").write_text(json.dumps({COMMAND_ID: {}}))
    assert handle(home, "deliver", control.encode(record()), now_ms=9000) == (0, {**IDENTITY, "state": "known"})
    assert not (home / "inbox").exists()


def test_deliver_answers_known_when_another_delivery_wins_the_link(home, monkeypatch):
    (home / "inbox").mkdir()
    (home / "inbox" / f"{COMMAND_ID}.json").write_text("{}")
    monkeypatch.setattr(control, "status", lambda command_id, inbox, state: "absent")
    assert handle(home, "deliver", control.encode(record())) == (0, {**IDENTITY, "state": "known"})
    assert sorted(path.name for path in (home / "inbox").iterdir()) == [f"{COMMAND_ID}.json"]
    assert (home / "inbox" / f"{COMMAND_ID}.json").read_text() == "{}"


def test_each_delivery_stages_under_its_own_process_name(home, monkeypatch):
    staged = []
    link = control.os.link
    monkeypatch.setattr(control.os, "getpid", lambda: 4242)
    monkeypatch.setattr(control.os, "link", lambda source, target: staged.append(source.name) or link(source, target))
    handle(home, "deliver", control.encode(record()))
    assert staged == [f"{COMMAND_ID}.4242.tmp"]


@pytest.mark.parametrize("mode", ["status", "deliver"])
def test_a_malformed_envelope_is_refused_with_its_reason(home, mode):
    assert handle(home, mode, "not base64!") == (2, refused("the envelope is not url safe base64", NO_IDENTITY))
    tampered = raw({**record(), "payload": {"text": "other"}})
    assert handle(home, mode, tampered) == (2, refused("the payload digest does not match", NO_IDENTITY))
    assert not (home / "inbox").exists()


@pytest.mark.parametrize("launch", [None, "not json", "[]", json.dumps({"execution_id": "x"})])
def test_a_command_is_refused_when_the_launch_record_is_unreadable(home, launch):
    if launch is None:
        (home / "launch.json").unlink()
    else:
        (home / "launch.json").write_text(launch)
    assert handle(home, "deliver", control.encode(record())) == (2, refused("the launch record is unreadable"))
    assert not (home / "inbox").exists()


@pytest.mark.parametrize("mode", ["status", "deliver"])
@pytest.mark.parametrize("changes", [{"execution_id": "exe-" + "2" * 32}, {"generation": 3}, {"generation": 5}])
def test_an_envelope_for_another_execution_or_generation_is_refused(home, mode, changes):
    envelope = record(**changes)
    assert handle(home, mode, control.encode(envelope)) == (2, refused("the envelope names another execution"))
    assert not (home / "inbox").exists()


def test_deliver_refuses_a_new_command_from_its_expiry_onward(home):
    envelope = control.encode(record())
    assert handle(home, "deliver", envelope, now_ms=5000) == (2, refused("the command expired"))
    assert not (home / "inbox").exists()
    assert handle(home, "deliver", envelope, now_ms=4999) == (0, {**IDENTITY, "state": "queued"})


def test_main_prints_one_sorted_json_line_and_uses_the_clock_in_milliseconds(home, capsys):
    envelope = control.encode(record())
    expected = refused("the command expired")
    assert run_main(home, capsys, ["deliver", envelope], now_ms=5000) == (
        2,
        json.dumps(dict(sorted(expected.items()))) + "\n",
    )
    assert run_main(home, capsys, ["deliver", envelope], now_ms=4999)[0] == 0
    code, out = run_main(home, capsys, ["status", envelope])
    assert (code, json.loads(out)) == (0, {**IDENTITY, "state": "queued"})


@pytest.mark.parametrize(
    "args",
    [[], ["status"], ["deliver"], ["status", "x", "extra"], ["sh", "-c"], ["exec", "x"], ["STATUS", "x"]],
)
def test_main_refuses_anything_but_one_mode_and_one_argument(home, capsys, args):
    code, out = run_main(home, capsys, args)
    assert (code, json.loads(out)) == (2, refused("usage: status ENVELOPE | deliver ENVELOPE", NO_IDENTITY))


def test_main_reads_sys_argv_by_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["control", "status", "x"])
    seen = []
    monkeypatch.setattr(control, "handle", lambda *args: seen.append(args[:2]) or (0, {}))
    assert control.main() == 0
    assert seen == [("status", "x")]


def test_main_defaults_to_the_fixed_helper_paths_and_wall_clock(monkeypatch):
    seen = []
    monkeypatch.setattr(control, "handle", lambda *args: seen.append(args) or (0, {}))
    monkeypatch.setattr(control.time, "time", lambda: 12.5)
    control.main(["deliver", "x"])
    assert seen == [("deliver", "x", control.INBOX, control.STATE, control.LAUNCH, 12500)]


class Transport:
    def __init__(self, refuse=None, unreachable=False):
        self.calls, self.refuse, self.unreachable = [], refuse, unreachable

    def poll(self):
        self.calls.append(["poll"])
        return []

    def ack(self, command_id, payload_digest):
        self.calls.append(["ack", command_id, payload_digest])
        if self.unreachable:
            raise ConnectionError("down")
        if self.refuse:
            raise control.CommandRefused(self.refuse, "refused")
        return {}

    def complete(self, command_id, outcome):
        self.calls.append(["complete", command_id, outcome])
        return {}


def worker(home, transport, ran):
    handlers = {"answer": lambda payload: ran.append(payload) or {"status": "succeeded"}}
    return control.WorkerControl(transport, home / "state.json", handlers, home / "inbox")


def test_worker_control_records_an_inbox_command_only_after_the_server_accepts_it(home):
    transport, ran = Transport(), []
    envelope = record()
    handle(home, "deliver", control.encode(envelope))
    (home / "inbox" / "note.json").write_text("{}")
    worker(home, transport, ran).step()
    assert ran == [envelope["payload"]]
    assert transport.calls == [
        ["ack", COMMAND_ID, envelope["payload_digest"]],
        ["complete", COMMAND_ID, {"status": "succeeded"}],
        ["poll"],
    ]
    assert sorted(path.name for path in (home / "inbox").iterdir()) == ["note.json"]
    assert json.loads((home / "state.json").read_bytes())[COMMAND_ID] == {
        "kind": "answer",
        "payload_digest": envelope["payload_digest"],
        "payload": envelope["payload"],
        "state": "reported",
        "outcome": {"status": "succeeded"},
        "inbox": True,
    }


def test_a_refused_inbox_drain_is_dropped_and_never_stops_mutations(home):
    handle(home, "deliver", control.encode(record(kind="drain", payload={})))
    refusing = worker(home, Transport(refuse="not_found"), [])
    refusing.step()
    assert refusing.records == {}
    assert refusing.may_mutate()
    assert list((home / "inbox").iterdir()) == []
    assert json.loads((home / "state.json").read_bytes()) == {}


def test_a_drain_refused_after_its_acceptance_stays_binding(home):
    handle(home, "deliver", control.encode(record(kind="drain", payload={})))

    class LateRefusal(Transport):
        def complete(self, command_id, outcome):
            raise control.CommandRefused("revision_conflict", "refused")

    draining = worker(home, LateRefusal(), [])
    draining.step()
    draining.checkpointed("refs/checkpoints/1")
    assert (draining.records[COMMAND_ID]["state"], draining.records[COMMAND_ID]["refusal"]) == (
        "rejected",
        "revision_conflict",
    )
    assert not draining.may_mutate()


def test_an_inbox_drain_accepted_by_the_server_stops_mutations_until_its_checkpoint(home):
    handle(home, "deliver", control.encode(record(kind="drain", payload={})))
    transport = Transport()
    draining = worker(home, transport, [])
    draining.step()
    assert not draining.may_mutate()
    assert draining.records[COMMAND_ID]["state"] == "accepted"
    draining.checkpointed("refs/checkpoints/1")
    assert transport.calls[-1] == [
        "complete",
        COMMAND_ID,
        {"status": "checkpointed", "checkpoint": "refs/checkpoints/1"},
    ]


def test_an_inbox_command_waits_in_the_worker_state_while_the_endpoint_is_unreachable(home):
    ran = []
    handle(home, "deliver", control.encode(record()))
    waiting = worker(home, Transport(unreachable=True), ran)
    waiting.step()
    assert (ran, waiting.records[COMMAND_ID]["state"], waiting.records[COMMAND_ID]["inbox"]) == ([], "received", True)
    assert list((home / "inbox").iterdir()) == []
    restarted = worker(home, Transport(), ran)
    restarted.step()
    assert ran == [record()["payload"]]


def test_a_received_inbox_drain_stops_mutations_before_its_acknowledgement(home):
    handle(home, "deliver", control.encode(record(kind="drain", payload={})))
    waiting = worker(home, Transport(unreachable=True), [])
    waiting.step()
    assert waiting.records[COMMAND_ID]["state"] == "received"
    assert not waiting.may_mutate()


def test_an_unreadable_inbox_file_is_left_for_the_next_step(home, monkeypatch):
    transport = Transport()
    handle(home, "deliver", control.encode(record()))

    def unreadable(path):
        raise PermissionError(str(path))

    monkeypatch.setattr(control.Path, "read_bytes", unreadable)
    blocked = control.WorkerControl(transport, home / "missing.json", {}, home / "inbox")
    blocked.step()
    assert (blocked.records, transport.calls) == ({}, [["poll"]])
    assert [path.name for path in (home / "inbox").iterdir()] == [f"{COMMAND_ID}.json"]


@pytest.mark.parametrize("content", ["[]", "{}", "not json", json.dumps({**record(), "payload": {"text": "other"}})])
def test_a_malformed_or_forged_inbox_file_is_dropped_without_blocking_the_poll(home, content):
    transport, ran = Transport(), []
    (home / "inbox").mkdir()
    (home / "inbox" / f"{COMMAND_ID}.json").write_text(content)
    worker(home, transport, ran).step()
    assert (ran, transport.calls) == ([], [["poll"]])
    assert list((home / "inbox").iterdir()) == []


def test_a_known_inbox_command_is_dropped_without_a_second_run(home):
    transport, ran = Transport(), []
    (home / "inbox").mkdir()
    (home / "state.json").write_text(json.dumps({}))
    known = worker(home, transport, ran)
    known.records[COMMAND_ID] = {
        "kind": "answer",
        "state": "reported",
        "outcome": {},
        "payload": {},
        "payload_digest": "",
    }
    (home / "inbox" / f"{COMMAND_ID}.json").write_text(json.dumps(record()))
    known.step()
    assert (ran, transport.calls) == ([], [["poll"]])
    assert list((home / "inbox").iterdir()) == []


def test_worker_control_without_an_inbox_only_polls(home, monkeypatch):
    transport = Transport()
    polling = control.WorkerControl(transport, home / "state.json", {})
    assert polling.inbox is None
    monkeypatch.setattr(control.Path, "glob", lambda *args: pytest.fail("no inbox to read"))
    polling.step()
    assert transport.calls == [["poll"]]
