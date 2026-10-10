import base64
import json
import sys

import pytest

from scripts.swarm_v2.runtime.operations import digest
from scripts.swarm_v2.worker import control

pytestmark = pytest.mark.unit

COMMAND_ID = "cmd-" + "a" * 32
EXECUTION = {"execution_id": "exe-" + "1" * 32, "generation": 4}


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


@pytest.fixture
def home(tmp_path):
    (tmp_path / "launch.json").write_text(json.dumps(EXECUTION))
    return tmp_path


def deliver(home, text, now_ms=4000):
    return control.deliver(text, home / "inbox", home / "state.json", home / "launch.json", now_ms)


def run_main(home, capsys, args, now_ms=4000):
    code = control.main(
        args, inbox=home / "inbox", state=home / "state.json", launch=home / "launch.json", clock=lambda: now_ms / 1000
    )
    return code, json.loads(capsys.readouterr().out)


def test_the_helper_paths_and_envelope_fields_are_fixed():
    assert str(control.INBOX) == "/home/worker/commands/inbox"
    assert str(control.STATE) == "/home/worker/commands/state.json"
    assert str(control.LAUNCH) == "/var/run/swarm/launch/launch.json"
    assert control.ENVELOPE == (
        "command_id",
        "execution_id",
        "generation",
        "kind",
        "payload",
        "payload_digest",
        "expires_at_ms",
    )
    assert control.USAGE == "usage: deliver ENVELOPE | status COMMAND_ID"


def test_encode_keeps_only_the_envelope_fields_as_url_safe_canonical_json():
    envelope = record()
    text = control.encode({**envelope, "state": "issued", "issued_at_ms": 1})
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    assert text == base64.urlsafe_b64encode(canonical).decode()
    assert set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    assert control.decode(text) == envelope


@pytest.mark.parametrize(
    "text, message",
    [
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


@pytest.mark.parametrize("text", ["!!!!", "é", "YWJj!", "ab+/"])
def test_decode_refuses_text_outside_the_url_safe_alphabet(text):
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
    assert deliver(home, control.encode(envelope)) == (0, {"command_id": COMMAND_ID, "state": "queued"})
    assert json.loads((home / "inbox" / f"{COMMAND_ID}.json").read_bytes()) == envelope
    assert deliver(home, control.encode(envelope)) == (0, {"command_id": COMMAND_ID, "state": "known"})
    assert sorted(path.name for path in (home / "inbox").iterdir()) == [f"{COMMAND_ID}.json"]


def test_deliver_answers_known_when_the_worker_already_holds_the_command(home):
    (home / "state.json").write_text(json.dumps({COMMAND_ID: {}}))
    assert deliver(home, control.encode(record())) == (0, {"command_id": COMMAND_ID, "state": "known"})
    assert not (home / "inbox").exists()


def test_deliver_answers_known_when_another_delivery_wins_the_link(home, monkeypatch):
    (home / "inbox").mkdir()
    (home / "inbox" / f"{COMMAND_ID}.json").write_text("{}")
    monkeypatch.setattr(control, "status", lambda command_id, inbox, state: "absent")
    assert deliver(home, control.encode(record())) == (0, {"command_id": COMMAND_ID, "state": "known"})
    assert sorted(path.name for path in (home / "inbox").iterdir()) == [f"{COMMAND_ID}.json"]
    assert (home / "inbox" / f"{COMMAND_ID}.json").read_text() == "{}"


def test_deliver_refuses_a_malformed_envelope_without_a_command_id(home):
    assert deliver(home, "not base64!") == (
        2,
        {"command_id": "", "state": "refused", "reason": "the envelope is malformed"},
    )
    assert deliver(home, raw({**record(), "payload": ["x"]})) == (
        2,
        {"command_id": "", "state": "refused", "reason": "the envelope is malformed"},
    )
    assert not (home / "inbox").exists()


@pytest.mark.parametrize("launch", [None, "not json", "[]", json.dumps({"execution_id": "x"})])
def test_deliver_refuses_when_the_launch_record_is_unreadable(home, launch):
    if launch is None:
        (home / "launch.json").unlink()
    else:
        (home / "launch.json").write_text(launch)
    assert deliver(home, control.encode(record())) == (
        2,
        {"command_id": COMMAND_ID, "state": "refused", "reason": "the launch record is unreadable"},
    )
    assert not (home / "inbox").exists()


@pytest.mark.parametrize("changes", [{"execution_id": "exe-" + "2" * 32}, {"generation": 3}, {"generation": 5}])
def test_deliver_refuses_an_envelope_for_another_execution_or_generation(home, changes):
    assert deliver(home, control.encode(record(**changes))) == (
        2,
        {"command_id": COMMAND_ID, "state": "refused", "reason": "the envelope names another execution"},
    )
    assert not (home / "inbox").exists()


def test_deliver_refuses_from_the_expiry_onward(home):
    envelope = control.encode(record())
    assert deliver(home, envelope, now_ms=5000) == (
        2,
        {"command_id": COMMAND_ID, "state": "refused", "reason": "the command expired"},
    )
    assert not (home / "inbox").exists()
    assert deliver(home, envelope, now_ms=4999) == (0, {"command_id": COMMAND_ID, "state": "queued"})


def test_main_runs_deliver_and_status_and_prints_one_sorted_json_line(home, capsys):
    envelope = control.encode(record())
    assert run_main(home, capsys, ["status", COMMAND_ID]) == (0, {"command_id": COMMAND_ID, "state": "absent"})
    code = control.main(
        ["deliver", envelope],
        inbox=home / "inbox",
        state=home / "state.json",
        launch=home / "launch.json",
        clock=lambda: 4.999,
    )
    assert (code, capsys.readouterr().out) == (0, json.dumps({"command_id": COMMAND_ID, "state": "queued"}) + "\n")
    assert run_main(home, capsys, ["status", COMMAND_ID]) == (0, {"command_id": COMMAND_ID, "state": "queued"})
    assert run_main(home, capsys, ["deliver", envelope]) == (0, {"command_id": COMMAND_ID, "state": "known"})


def test_main_passes_the_clock_in_milliseconds(home, capsys):
    assert run_main(home, capsys, ["deliver", control.encode(record())], now_ms=5000) == (
        2,
        {"command_id": COMMAND_ID, "reason": "the command expired", "state": "refused"},
    )


@pytest.mark.parametrize(
    "args",
    [[], ["status"], ["deliver"], ["status", COMMAND_ID, "extra"], ["sh", "-c"], ["exec", COMMAND_ID]],
)
def test_main_refuses_anything_but_one_mode_and_one_argument(home, capsys, args):
    assert run_main(home, capsys, args) == (
        2,
        {"command_id": "", "reason": "usage: deliver ENVELOPE | status COMMAND_ID", "state": "refused"},
    )


@pytest.mark.parametrize("command_id", ["cmd-short", "$(id)", COMMAND_ID + "x", "../" + COMMAND_ID])
def test_main_refuses_a_malformed_status_id(home, capsys, command_id):
    assert run_main(home, capsys, ["status", command_id]) == (
        2,
        {"command_id": "", "reason": "the command id is malformed", "state": "refused"},
    )


def test_main_reads_sys_argv_by_default(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["control", "status", COMMAND_ID])
    monkeypatch.setattr(control, "status", lambda command_id, inbox, state: "absent")
    assert control.main() == 0
    assert json.loads(capsys.readouterr().out) == {"command_id": COMMAND_ID, "state": "absent"}


def test_main_defaults_to_the_fixed_helper_paths(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(control, "status", lambda command_id, inbox, state: seen.append((inbox, state)) or "absent")
    monkeypatch.setattr(
        control,
        "deliver",
        lambda text, inbox, state, launch, now_ms: seen.append((inbox, state, launch, now_ms)) or (0, {}),
    )
    monkeypatch.setattr(control.time, "time", lambda: 12.5)
    control.main(["status", COMMAND_ID])
    control.main(["deliver", "x"])
    assert seen == [
        (control.INBOX, control.STATE),
        (control.INBOX, control.STATE, control.LAUNCH, 12500),
    ]


class Transport:
    def __init__(self):
        self.calls = []

    def poll(self):
        self.calls.append(["poll"])
        return []

    def ack(self, command_id, payload_digest):
        self.calls.append(["ack", command_id, payload_digest])
        return {}

    def complete(self, command_id, outcome):
        self.calls.append(["complete", command_id, outcome])
        return {}


def test_worker_control_takes_inbox_commands_and_still_acknowledges_before_running(home):
    transport, ran = Transport(), []
    inbox = home / "inbox"
    envelope = record()
    assert deliver(home, control.encode(envelope))[0] == 0
    (inbox / "note.json").write_text("{}")
    worker = control.WorkerControl(
        transport,
        home / "state.json",
        {"answer": lambda payload: ran.append(payload) or {"status": "succeeded"}},
        inbox,
    )
    worker.step()
    assert ran == [envelope["payload"]]
    assert transport.calls == [
        ["ack", COMMAND_ID, envelope["payload_digest"]],
        ["complete", COMMAND_ID, {"status": "succeeded"}],
        ["poll"],
    ]
    assert sorted(path.name for path in inbox.iterdir()) == ["note.json"]
    assert json.loads((home / "state.json").read_bytes())[COMMAND_ID]["state"] == "reported"


def test_worker_control_waits_for_acknowledgement_when_the_endpoint_is_unreachable(home):
    class Unreachable(Transport):
        def ack(self, command_id, payload_digest):
            raise ConnectionError("down")

    ran = []
    deliver(home, control.encode(record()))
    worker = control.WorkerControl(Unreachable(), home / "state.json", {"answer": ran.append}, home / "inbox")
    worker.step()
    assert ran == []
    assert worker.records[COMMAND_ID]["state"] == "received"
    assert list((home / "inbox").iterdir()) == []


def test_worker_control_drops_a_known_or_forged_inbox_command_without_running_it(home):
    transport, ran = Transport(), []
    inbox = home / "inbox"
    inbox.mkdir()
    forged = {**record(command_id="cmd-" + "f" * 32), "payload": {"text": "other"}}
    (inbox / f"{forged['command_id']}.json").write_text(json.dumps(forged))
    (home / "state.json").write_text(json.dumps({}))
    worker = control.WorkerControl(transport, home / "state.json", {"answer": ran.append}, inbox)
    worker.records[COMMAND_ID] = {
        "kind": "answer",
        "state": "reported",
        "outcome": {},
        "payload": {},
        "payload_digest": "",
    }
    (inbox / f"{COMMAND_ID}.json").write_text(json.dumps(record()))
    worker.step()
    assert ran == []
    assert transport.calls == [["poll"]]
    assert list(inbox.iterdir()) == []
    assert sorted(worker.records) == [COMMAND_ID]


def test_worker_control_without_an_inbox_only_polls(home, monkeypatch):
    transport = Transport()
    worker = control.WorkerControl(transport, home / "state.json", {})
    assert worker.inbox is None
    monkeypatch.setattr(control.Path, "glob", lambda *args: pytest.fail("no inbox to read"))
    worker.step()
    assert transport.calls == [["poll"]]
