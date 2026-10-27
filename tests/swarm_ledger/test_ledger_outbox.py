import io
import itertools
import json
import sys
import threading
import urllib.error
from http.server import ThreadingHTTPServer

import ledger_core as core
import pytest

from scripts.inbox import outbox
from scripts.swarm_ledger import ledger, ledger_server, new_ledger

SLUG = "outbox-2026-01-01"
ENG = "eng-1@demo"
BOSS = "master@demo"
OPERATOR_LINE = "o-0"
COUNT = itertools.count(1)
QUEUED = "queued locally, it replays when the server returns"


class Server:
    """The ledger server on a spare port; stop refuses every connection until start binds a new one."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.httpd = None

    def start(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
        port = self.httpd.server_port
        self.monkeypatch.setattr(ledger_server, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"})
        self.monkeypatch.setattr(ledger, "BASE", f"http://127.0.0.1:{port}")
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch, request):
    monkeypatch.setattr(sys.modules[__name__], "SLUG", f"outbox-{len(request.node.name)}-{next(COUNT)}-2026-01-01")
    monkeypatch.setenv("LEDGER_AUTOSTART", "0")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    new_ledger.create(SLUG, {"title": "Outbox", "overview": "o", "phases": [{"title": "one"}], "questions": []})
    served = Server(monkeypatch)
    served.start()
    ledger.call(SLUG, [{"op": "join", "id": "join-boss", "by": BOSS, "role": "orchestrator"}])
    ledger.call(SLUG, [{"op": "add", "thread": "chat", "id": OPERATOR_LINE, "text": "Carry on"}])
    yield served
    served.stop()


def run(capsys, *argv):
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", argv[0], *argv[1:]])
    getattr(ledger, f"cmd_{args.command}")(args)
    captured = capsys.readouterr()
    run.err = captured.err
    return [json.loads(line) for line in captured.out.splitlines()]


def queued():
    if not outbox.path().exists():
        return []
    return [json.loads(line) for line in outbox.path().read_text().splitlines()]


def stored():
    state, _ = core.sync(SLUG)
    return state


def comments(state):
    return [entry["text"] for entry in state["phases"][0]["comments"]]


def chat(state):
    return [entry["text"] for entry in state["chat"] if entry["by"] == ENG]


def test_talk_sent_while_the_server_is_stopped_queues_and_lands_once_in_order_when_it_returns(server, capsys):
    phase = f"phases/{stored()['phases'][0]['id']}"
    server.stop()
    assert run(capsys, ENG, "comment", phase, "The branch builds on the new base") == [
        {"posted": False, "queued": True}
    ]
    assert run.err == f"ledger server not answering on {ledger.BASE}: {QUEUED}\n"
    assert run(capsys, ENG, "say", "Tests run in continuous integration now", "--reply-to", OPERATOR_LINE) == [
        {"posted": False, "queued": True}
    ]
    assert run(capsys, ENG, "question", "add", "Which port should the service use") == [
        {"question": "add", "queued": True}
    ]
    records = queued()
    assert [record["op"] for record in records] == ["ledger"] * 3
    assert {(record["by"], record["args"]["agent"], record["args"]["service"]) for record in records} == {
        ("", "", False)
    }
    assert [record["args"]["ops"][0]["op"] for record in records] == ["add", "add", "add_item"]
    assert [record["item"] for record in records] == [record["args"]["ops"][0]["id"] for record in records]
    assert comments(stored()) == []
    assert chat(stored()) == []
    assert stored()["questions"] == []

    server.start()
    ledger.call(SLUG)
    state = stored()
    assert comments(state) == ["The branch builds on the new base"]
    assert chat(state) == ["Tests run in continuous integration now"]
    assert [question["text"] for question in state["questions"]] == ["Which port should the service use"]
    assert queued() == []
    assert not outbox.path().exists()


def test_a_replayed_record_lands_once_even_when_it_is_replayed_again(server, capsys):
    phase = f"phases/{stored()['phases'][0]['id']}"
    server.stop()
    run(capsys, ENG, "comment", phase, "Only one copy of this lands")
    line = outbox.path().read_text()
    server.start()
    assert outbox.flush(None).applied == [json.loads(line)["item"]]
    outbox.path().write_text(line)
    assert outbox.flush(None).applied == [json.loads(line)["item"]]
    assert comments(stored()) == ["Only one copy of this lands"]
    assert not outbox.path().exists()


def test_an_answer_sent_while_the_server_is_stopped_lands_as_the_master_when_it_returns(server, capsys, monkeypatch):
    monkeypatch.setattr(ledger, "swarm_autonomy", lambda slug: "delegate")
    run(capsys, ENG, "question", "add", "Which broker should we use")
    question = f"questions/{stored()['questions'][0]['id']}"
    server.stop()
    assert run(capsys, BOSS, "answer", question, "Use the existing broker") == [
        {"answered": False, "item": question, "queued": True}
    ]
    assert queued()[0]["args"]["agent"] == ""
    assert queued()[0]["args"]["ops"][0]["op"] == "answer"
    server.start()
    ledger.call(SLUG)
    answers = stored()["questions"][0]["answers"]
    assert [(entry["by"], entry["text"]) for entry in answers] == [(BOSS, "Use the existing broker")]


def test_talk_sent_after_the_server_returns_lands_after_the_talk_queued_before_it(server, capsys):
    phase = f"phases/{stored()['phases'][0]['id']}"
    server.stop()
    run(capsys, ENG, "say", "First message", "--reply-to", OPERATOR_LINE)
    run(capsys, ENG, "say", "Second message", "--reply-to", OPERATOR_LINE)
    assert [record["args"]["ops"][0]["text"] for record in queued()] == ["First message", "Second message"]
    server.start()
    run(capsys, ENG, "comment", phase, "Third arrives after both")
    state = stored()
    assert chat(state) == ["First message", "Second message"]
    assert comments(state) == ["Third arrives after both"]
    assert queued() == []


def test_a_record_the_server_refuses_on_replay_is_set_aside_and_reported(server, capsys):
    server.stop()
    bad = {"op": "add", "thread": "phases/missing/comments", "id": "c-missing", "text": "Lost item", "by": ENG}
    assert ledger.call(SLUG, [bad])["queued"] == "c-missing"
    server.start()
    report = outbox.flush(None)
    assert report.rejected and report.rejected[0][0] == "c-missing"
    assert not outbox.path().exists()
    assert json.loads(outbox.skipped_path().read_text().splitlines()[-1])["line"].startswith('{"op": "ledger"')
    assert "refused on replay" in capsys.readouterr().err


def test_reads_and_untalkative_writes_still_fail_while_the_server_is_stopped(server):
    server.stop()
    with pytest.raises(SystemExit, match="ledger server not answering"):
        ledger.call(SLUG)
    with pytest.raises(SystemExit, match="ledger server not answering"):
        ledger.call(SLUG, [{"op": "leave", "id": "leave-1", "by": ENG}])
    assert queued() == []


def test_an_untalkative_write_is_refused_while_queued_talk_still_waits_so_it_never_overtakes_it(server, capsys):
    server.stop()
    run(capsys, ENG, "say", "Queued first", "--reply-to", OPERATOR_LINE)
    held = outbox.path().read_text()
    with pytest.raises(SystemExit, match="still waits in"):
        ledger.call(SLUG, [{"op": "join", "id": "join-eng", "by": ENG, "role": "member"}])
    assert outbox.path().read_text() == held


def test_a_busy_or_conflicting_server_answer_keeps_the_record_for_a_later_replay(server, monkeypatch, capsys):
    import urllib.error

    server.stop()
    run(capsys, ENG, "say", "Kept through a busy server", "--reply-to", OPERATOR_LINE)
    held = outbox.path().read_text()
    revision = json.dumps({"error": {"code": "revision_conflict", "message": "persisted"}}).encode()
    for code, body in ((503, b""), (500, b""), (429, b""), (409, revision)):

        def busy(*args, code=code, body=body):
            raise urllib.error.HTTPError("http://ledger", code, "busy", {}, io.BytesIO(body))

        monkeypatch.setattr(ledger, "replay", busy)
        report = outbox.flush(None)
        assert (report.applied, report.rejected, report.left) == ([], [], 1)
        assert outbox.path().read_text() == held
    assert not outbox.skipped_path().exists()

    def conflict(*args):
        body = json.dumps({"error": {"code": "operation_conflict"}}).encode()
        raise urllib.error.HTTPError("http://ledger", 409, "operation conflict", {}, io.BytesIO(body))

    monkeypatch.setattr(ledger, "replay", conflict)
    report = outbox.flush(None)
    assert report.rejected == [(json.loads(held)["item"], "the ledger server refused it: 409")]
    assert not outbox.path().exists()


def aged(seconds):
    entry = queued()[0]
    entry["args"]["queued_at"] -= seconds
    body = {key: value for key, value in entry.items() if key != "sum"}
    outbox.path().write_text(json.dumps({**body, "sum": outbox.checksum(body)}) + "\n")
    return entry


@pytest.mark.parametrize(("seconds", "kept"), [(59, True), (61, False)])
def test_a_record_the_server_keeps_answering_busy_is_set_aside_once_it_is_older_than_the_limit(
    server, monkeypatch, capsys, seconds, kept
):
    import urllib.error

    server.stop()
    from types import SimpleNamespace

    run(capsys, ENG, "say", "Busy for too long", "--reply-to", OPERATOR_LINE)
    entry = aged(seconds)
    now = entry["args"]["queued_at"] + seconds
    monkeypatch.setattr(outbox, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setenv(outbox.BUSY_AGE_ENV, "60")

    def busy(*args):
        raise urllib.error.HTTPError("http://ledger", 503, "busy", {}, None)

    monkeypatch.setattr(ledger, "replay", busy)
    report = outbox.flush(None)
    assert outbox.path().exists() is kept
    if kept:
        assert (report.left, report.rejected) == (1, [])
        return
    reason = f"the ledger server answered 503 for over 60 seconds ({outbox.BUSY_AGE_ENV})"
    assert report.rejected == [(entry["item"], reason)]
    assert json.loads(outbox.skipped_path().read_text().splitlines()[-1])["reason"] == reason


def test_a_stopped_server_never_expires_held_talk(server, capsys):
    server.stop()
    run(capsys, ENG, "say", "Held through a long outage", "--reply-to", OPERATOR_LINE)
    aged(10 * outbox.DEFAULT_BUSY_AGE)
    report = outbox.flush(None)
    assert (report.left, report.rejected) == (1, [])


def test_the_busy_age_limit_reads_its_setting_and_defaults_to_an_hour():
    assert outbox.busy_age({}) == 3600
    assert outbox.busy_age({outbox.BUSY_AGE_ENV: "90"}) == 90


def test_a_record_is_stale_only_past_the_limit(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(outbox, "time", SimpleNamespace(time=lambda: 1000.0))
    monkeypatch.setenv(outbox.BUSY_AGE_ENV, "60")
    assert not outbox.stale({"args": {"queued_at": 940.0}})
    assert outbox.stale({"args": {"queued_at": 939.0}})


def test_a_failing_ledger_record_holds_its_later_ledger_records_and_never_the_inbox_lane(server, monkeypatch, capsys):
    from scripts.inbox import cli

    server.stop()
    run(capsys, ENG, "say", "First held", "--reply-to", OPERATOR_LINE)
    run(capsys, ENG, "say", "Second held", "--reply-to", OPERATOR_LINE)
    sent = outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False})
    outbox.append(sent)
    calls, applied = [], []
    monkeypatch.setitem(cli.APPLY, "send", lambda store, by, entry: applied.append(entry["item"]))
    for failure in (urllib.error.HTTPError("http://ledger", 503, "busy", {}, io.BytesIO(b"")), RuntimeError("odd")):

        def failing(*args, failure=failure):
            calls.append(args)
            raise failure

        monkeypatch.setattr(ledger, "replay", failing)
        calls.clear()
        outbox.flush(object())
        assert len(calls) == 1
        assert [record["op"] for record in queued()] == ["ledger", "ledger"]
    assert applied == [sent["item"]]


def test_a_flush_without_a_store_keeps_inbox_records_untried(server, capsys):
    sent = outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False})
    outbox.append(sent)
    report = outbox.flush(None)
    assert (report.applied, report.left) == ([], 1)
    assert "outbox replay stopped" not in capsys.readouterr().err


def test_an_expired_record_is_set_aside_as_it_was_written(server, monkeypatch, capsys):
    server.stop()
    run(capsys, ENG, "say", "Expired verbatim", "--reply-to", OPERATOR_LINE)
    aged(10)
    line = outbox.path().read_text()
    monkeypatch.setenv(outbox.BUSY_AGE_ENV, "5")
    monkeypatch.setattr(
        ledger, "replay", lambda *args: (_ for _ in ()).throw(urllib.error.HTTPError("u", 502, "", {}, io.BytesIO(b"")))
    )
    outbox.flush(None)
    assert json.loads(outbox.skipped_path().read_text().splitlines()[-1])["line"] == line


def test_apply_ledger_replays_as_the_recorded_author_and_reports_the_outcome(monkeypatch):
    calls = []
    monkeypatch.setattr(ledger, "replay", lambda *args: calls.append(args) or {"rejected": ["c-1", "c-2"]})
    entry = outbox.ledger_record(SLUG, [{"op": "add", "id": "c-1"}], ENG, True)
    with pytest.raises(outbox.InboxError) as refused:
        outbox.apply_ledger(None, ENG, entry)
    assert str(refused.value) == "the ledger server rejected c-1, c-2"
    assert calls == [(SLUG, [{"op": "add", "id": "c-1"}], ENG, True)]
    monkeypatch.setattr(ledger, "replay", lambda *args: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    with pytest.raises(outbox.LedgerDown) as down:
        outbox.apply_ledger(None, ENG, entry)
    assert str(down.value) == "refused"
    monkeypatch.setattr(ledger, "replay", lambda *args: {"applied": ["c-1"]})
    assert outbox.apply_ledger(None, ENG, entry) == {"applied": ["c-1"]}


def test_ledger_replay_sends_the_recorded_credential_with_the_request_timeout(monkeypatch):
    seen = []

    class Client:
        def __init__(self, base, headers, timeout):
            seen.append((headers, timeout))

        def mutate(self, slug, ops):
            return {"slug": slug, "ops": ops}

    monkeypatch.setattr("scripts.swarm_ledger.api.client.ResourceClient", Client)
    monkeypatch.setattr(ledger.repository, "token", lambda slug: "admin")
    assert ledger.replay(SLUG, ["op"], ENG, False) == {"slug": SLUG, "ops": ["op"]}
    assert seen[-1] == (ledger.local_credentials(SLUG, ENG, False), ledger.REQUEST_TIMEOUT)
    assert seen[-1][0]["X-Ledger-Agent"] == ENG
    ledger.replay(SLUG, ["op"], ENG, True)
    assert seen[-1][0] == {"X-Ledger-Token": "admin"}


def test_talk_is_agent_chat_comments_items_answers_and_relays_without_attachments():
    assert ledger.talk([{"op": "add", "by": ENG, "thread": "chat"}])
    assert not ledger.talk([{"op": "add", "by": ENG, "thread": "chat", "attachments": [{"id": "m"}]}])
    assert not ledger.talk([{"op": "add", "thread": "chat"}])
    assert ledger.talk([{"op": name} for name in ledger.TALK_OPS])
    assert not ledger.talk([{"op": "join"}])
    assert not ledger.talk([])


def test_an_inbox_record_waiting_never_holds_ledger_writes(server, capsys):
    outbox.append(outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False}))
    phase = f"phases/{stored()['phases'][0]['id']}"
    assert run(capsys, ENG, "comment", phase, "Posted past an inbox record") == [{"posted": True}]
    assert "wait in" not in run.err
    assert comments(stored()) == ["Posted past an inbox record"]


def test_a_full_outbox_refuses_the_write_with_its_reason(server, monkeypatch, capsys):
    server.stop()
    monkeypatch.setenv(outbox.MAX_ENV, "10")
    with pytest.raises(SystemExit) as full:
        ledger.call(SLUG, [{"op": "add", "thread": "chat", "id": "m-1", "text": "Too big", "by": ENG}])
    assert str(full.value).startswith("the local inbox outbox is full (0 of 10 bytes")


def test_a_ledger_outage_does_not_hold_a_queued_inbox_record(server, capsys, monkeypatch):
    from scripts.inbox import cli

    server.stop()
    run(capsys, ENG, "say", "Waits for the ledger", "--reply-to", OPERATOR_LINE)
    sent = outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False})
    outbox.append(sent)
    applied = []
    monkeypatch.setitem(cli.APPLY, "send", lambda store, by, entry: applied.append(entry["item"]))
    report = outbox.flush(object())
    assert applied == [sent["item"]] and report.applied == [sent["item"]]
    assert [record["op"] for record in queued()] == ["ledger"]


def test_a_redis_record_and_a_ledger_record_replay_independently(server, capsys):
    server.stop()
    inbox = outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False})
    outbox.append(inbox)
    run(capsys, ENG, "say", "Ledger talk behind an inbox send", "--reply-to", OPERATOR_LINE)
    server.start()
    report = outbox.flush(None)
    assert chat(stored()) == ["Ledger talk behind an inbox send"]
    assert [record["item"] for record in queued()] == [inbox["item"]]
    assert report.left == 1


def test_an_inbox_send_goes_live_past_waiting_ledger_talk_and_the_inbox_flush_replays_it(server, capsys):
    server.stop()
    run(capsys, ENG, "say", "Replayed by the inbox", "--reply-to", OPERATOR_LINE)
    live = []
    found = outbox.submit(object(), outbox.record("send", ENG, {"text": "x"}), lambda store, entry: live.append(entry))
    assert found is None and len(live) == 1
    assert len(queued()) == 1
    server.start()
    assert len(outbox.flush(object()).applied) == 1
    assert chat(stored()) == ["Replayed by the inbox"]
    assert not outbox.path().exists()


STALE = "0" * 64


def chat_op(item, text):
    return {
        "op": "add",
        "thread": "chat",
        "id": item,
        "text": text,
        "by": ENG,
        "to": "operator",
        "reply_to": OPERATOR_LINE,
    }


def queue_ops(*ops):
    for op in ops:
        outbox.append(outbox.ledger_record(SLUG, [op], "", False))


def revision_conflict(where=None):
    details = {"path": where} if where else {}
    body = json.dumps({"error": {"code": "revision_conflict", "message": "changed", "details": details}}).encode()
    return urllib.error.HTTPError("http://ledger", 409, "conflict", {}, io.BytesIO(body))


def test_a_queued_comment_with_a_stale_revision_lands_with_a_fresh_one_and_the_writes_behind_it_follow(server):
    phase = f"phases/{stored()['phases'][0]['id']}"
    ledger.call(SLUG, [{"op": "add", "thread": f"{phase}/comments", "id": "c-moved", "text": "Moved it", "by": BOSS}])
    stale_comment = {"op": "add", "thread": f"{phase}/comments", "id": "c-stale", "text": "Queued earlier", "by": ENG}
    queue_ops({**stale_comment, "expected_revision": STALE}, chat_op("m-behind", "Queued behind the comment"))
    report = outbox.flush(None)
    assert (report.applied, report.rejected, report.left) == (["c-stale", "m-behind"], [], 0)
    assert comments(stored()) == ["Moved it", "Queued earlier"]
    assert chat(stored()) == ["Queued behind the comment"]
    assert not outbox.path().exists()
    assert not outbox.skipped_path().exists()


@pytest.mark.parametrize("kind", ["add", "add_item", "relay", "answer"])
def test_an_append_only_op_with_a_stale_revision_is_sent_once_more_with_a_fresh_one(monkeypatch, kind):
    sent = []

    def replay(slug, ops, agent, service):
        sent.append([dict(op) for op in ops])
        if ops[0].get("expected_revision"):
            raise revision_conflict("chat")
        return {"applied": [ops[0]["id"]]}

    monkeypatch.setattr(ledger, "replay", replay)
    entry = outbox.ledger_record(SLUG, [{"op": kind, "id": "x-1", "expected_revision": STALE}], ENG, False)
    assert outbox.apply_ledger(None, ENG, entry) == {"applied": ["x-1"]}
    assert sent == [[{"op": kind, "id": "x-1", "expected_revision": STALE}], [{"op": kind, "id": "x-1"}]]
    assert entry["args"]["ops"] == [{"op": kind, "id": "x-1", "expected_revision": STALE}]


def test_a_queued_write_that_is_not_an_append_with_a_stale_revision_is_set_aside_at_once(server, monkeypatch):
    sent = []

    def replay(slug, ops, agent, service):
        sent.append(ops[0]["id"])
        if any(op.get("expected_revision") for op in ops):
            raise revision_conflict("phases/p1")
        return {"applied": [op["id"] for op in ops]}

    monkeypatch.setattr(ledger, "replay", replay)
    rename = {"op": "set", "path": "phases/p1/title", "value": "Renamed", "id": "s-stale", "expected_revision": STALE}
    queue_ops(rename, chat_op("m-after", "Lands behind the stale write"))
    line = outbox.path().read_text().splitlines(keepends=True)[0]
    report = outbox.flush(None)
    reason = (
        "the ledger server answered 409 revision conflict on phases/p1 since this write was queued, "
        "so its expected revision can never apply"
    )
    assert report.rejected == [("s-stale", reason)]
    assert report.applied == ["m-after"]
    assert sent == ["s-stale", "m-after"]
    assert not outbox.path().exists()
    assert json.loads(outbox.skipped_path().read_text().splitlines()[-1]) == {"reason": reason, "line": line}


def test_a_record_still_conflicting_holds_only_later_records_for_its_own_resource(server, monkeypatch):
    sent = []

    def replay(slug, ops, agent, service):
        sent.append([dict(op) for op in ops])
        if ops[0]["thread"] == "phases/a/comments":
            raise revision_conflict()
        return {"applied": [op["id"] for op in ops]}

    monkeypatch.setattr(ledger, "replay", replay)
    first = {"op": "add", "thread": "phases/a/comments", "id": "c-a1", "text": "First on a", "by": ENG}
    second = {"op": "add", "thread": "phases/a/comments", "id": "c-a2", "text": "Second on a", "by": ENG}
    queue_ops({**first, "expected_revision": STALE}, chat_op("m-b", "Another item"), second)
    report = outbox.flush(None)
    assert (report.applied, report.rejected, report.left) == (["m-b"], [], 2)
    assert [ops[0]["id"] for ops in sent] == ["c-a1", "c-a1", "m-b"]
    assert "expected_revision" not in sent[1][0]
    assert [record["item"] for record in queued()] == ["c-a1", "c-a2"]
    assert queued()[0]["args"]["ops"][0]["expected_revision"] == STALE


def test_an_unpinned_record_still_conflicting_lets_inbox_records_and_other_items_replay(server, monkeypatch):
    from scripts.inbox import cli

    def replay(slug, ops, agent, service):
        if ops[0]["thread"] == "phases/a/comments":
            raise revision_conflict("phases/a/comments")
        return {"applied": [op["id"] for op in ops]}

    monkeypatch.setattr(ledger, "replay", replay)
    sends = []
    monkeypatch.setitem(cli.APPLY, "send", lambda store, by, entry: sends.append(entry["item"]))
    first = {"op": "add", "thread": "phases/a/comments", "id": "c-a1", "text": "First on a", "by": ENG}
    queue_ops(first)
    sent = outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False})
    outbox.append(sent)
    queue_ops(chat_op("m-b", "Another item"), {**first, "id": "c-a2", "text": "Second on a"})
    report = outbox.flush(object())
    assert (report.applied, report.rejected, report.left) == ([sent["item"], "m-b"], [], 2)
    assert sends == [sent["item"]]
    assert [record["item"] for record in queued()] == ["c-a1", "c-a2"]
    assert outbox.targets(sent) == frozenset()


def always_raises(make):
    def replay(*args):
        raise make()

    return replay


def test_a_record_kept_behind_a_conflict_holds_every_resource_it_writes(server, monkeypatch):
    sent = []

    def replay(slug, ops, agent, service):
        sent.append(ops[0]["id"])
        raise revision_conflict("chat")

    monkeypatch.setattr(ledger, "replay", replay)
    comment = {"op": "add", "thread": "phases/a/comments", "id": "c-a", "text": "On a", "by": ENG}
    queue_ops(chat_op("m-1", "Conflicting chat"))
    outbox.append(outbox.ledger_record(SLUG, [chat_op("m-2", "Chat and comment"), comment], "", False))
    queue_ops({**comment, "id": "c-a2"})
    report = outbox.flush(None)
    assert (report.applied, report.left) == ([], 3)
    assert sent == ["m-1"]


def test_a_ledger_record_whose_resource_cannot_be_read_is_set_aside_and_the_records_behind_it_replay(
    server, monkeypatch
):
    from scripts.inbox import cli

    sends = []
    monkeypatch.setitem(cli.APPLY, "send", lambda store, by, entry: sends.append(entry["item"]))
    queue_ops({"op": "add", "id": "c-bad", "text": "No thread", "by": ENG})
    sent = outbox.record("send", ENG, {"address": "bob", "text": "for bob", "fyi": False})
    outbox.append(sent)
    report = outbox.flush(object())
    assert report.rejected == [("c-bad", "'thread'")]
    assert sends == [sent["item"]]
    assert not outbox.path().exists()


def test_a_revision_conflict_names_the_resource_the_server_reports(monkeypatch):
    entry = outbox.ledger_record(SLUG, [chat_op("m-1", "Unpinned")], ENG, False)
    for where, message in ((None, "the ledger server answered 409 revision conflict"), ("chat", "on chat")):
        monkeypatch.setattr(ledger, "replay", always_raises(lambda where=where: revision_conflict(where)))
        with pytest.raises(outbox.LedgerBusy) as busy:
            outbox.apply_ledger(None, ENG, entry)
        assert str(busy.value).endswith(message)
        assert busy.value.targets == {"chat"}


def test_a_pinned_append_still_conflicting_after_its_refresh_is_busy_on_its_own_resource(monkeypatch):
    monkeypatch.setattr(ledger, "replay", always_raises(lambda: revision_conflict("chat")))
    entry = outbox.ledger_record(SLUG, [{**chat_op("m-1", "Pinned"), "expected_revision": STALE}], ENG, False)
    with pytest.raises(outbox.LedgerBusy) as busy:
        outbox.apply_ledger(None, ENG, entry)
    assert str(busy.value) == "the ledger server answered 409 revision conflict on chat"
    assert busy.value.targets == {"chat"}


def test_a_server_wide_busy_answer_holds_every_later_ledger_record(server, monkeypatch):
    monkeypatch.setattr(
        ledger, "replay", always_raises(lambda: urllib.error.HTTPError("u", 503, "", {}, io.BytesIO(b"")))
    )
    queue_ops(
        chat_op("m-1", "First"), {"op": "add", "thread": "phases/a/comments", "id": "c-2", "text": "Else", "by": ENG}
    )
    assert outbox.flush(None).left == 2
    with pytest.raises(outbox.LedgerBusy) as busy:
        outbox.apply_ledger(None, ENG, queued()[0])
    assert busy.value.targets == frozenset()
