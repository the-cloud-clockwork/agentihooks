import json
from contextlib import contextmanager

import pytest

from hooks import config
from scripts.inbox import cli, outbox
from scripts.inbox.store import Contended, DuplicateItem, InboxStore


def lost(text):
    import redis

    return redis.ConnectionError(text)


class Outage:
    """Redis as the CLI sees it: refused while down, else one fakeredis whose sends can fail mid pipeline."""

    def __init__(self, monkeypatch):
        import fakeredis

        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.down = False
        self.failures = []
        self.environs = []
        monkeypatch.setattr("scripts.swarm.store.redis_client", self.client)
        original = type(self.redis.pipeline()).execute

        def execute(pipe, *args, **kwargs):
            writing = any(command[0][0] == "RPUSH" for command in pipe.command_stack)
            mode = self.failures.pop(0) if writing and self.failures else ""
            if mode == "before":
                raise lost("connection lost before the send landed")
            result = original(pipe, *args, **kwargs)
            if mode == "after":
                raise lost("connection lost after the send landed")
            return result

        monkeypatch.setattr(type(self.redis.pipeline()), "execute", execute)

    def client(self, environ=None):
        self.environs.append(environ)
        if self.down:
            raise lost("Error 111 connecting to localhost:6379. Connection refused.")
        return self.redis

    def texts(self, address):
        return [item.text for item in InboxStore(self.redis).inbox(address)]


@pytest.fixture
def outage(monkeypatch):
    monkeypatch.setattr(cli, "registered_name", lambda: "")
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions",
        lambda **kwargs: {"sess-a": {"name": "alice"}, "sess-b": {"name": "bob"}},
    )
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "alice")
    return Outage(monkeypatch)


def run(*argv):
    return cli.main(list(argv))


def records():
    return [json.loads(line) for line in outbox.path().read_text().splitlines()] if outbox.path().exists() else []


def test_sends_during_a_refused_connection_arrive_once_in_send_order_when_redis_returns(outage, monkeypatch, capsys):
    outage.down = True
    for text in ("first", "second", "third"):
        assert run("send", "bob", text) == 0
    out, err = capsys.readouterr()
    queued = [json.loads(line) for line in out.splitlines()]
    assert [row["state"] for row in queued] == ["queued"] * 3
    assert "queued locally" in err
    assert [record["args"]["text"] for record in records()] == ["first", "second", "third"]
    assert [record["item"] for record in records()] == [row["id"] for row in queued]

    outage.down = False
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("inbox") == 0
    assert outage.texts("bob") == ["first", "second", "third"]
    assert [item.id for item in InboxStore(outage.redis).inbox("bob")] == [row["id"] for row in queued]
    assert records() == []
    assert run("inbox") == 0
    assert outage.texts("bob") == ["first", "second", "third"]


def test_sends_failing_mid_pipeline_arrive_exactly_once_in_send_order(outage, capsys):
    outage.failures = ["after", "before"]
    for text in ("one", "two", "three"):
        assert run("send", "bob", text) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["state"] for row in rows] == ["queued", "queued", "pending"]
    assert (rows[2]["from"], rows[2]["to"]) == ("alice", "bob")
    assert records() == []
    assert outage.texts("bob") == ["one", "two", "three"]
    assert outbox.flush(InboxStore(outage.redis)).applied == []
    assert outage.texts("bob") == ["one", "two", "three"]


def test_a_send_waits_behind_queued_records_while_they_cannot_replay(outage, capsys):
    outage.failures = ["before", "before"]
    assert run("send", "bob", "one") == 0
    assert run("send", "bob", "two") == 0
    assert [json.loads(line)["state"] for line in capsys.readouterr().out.splitlines()] == ["queued", "queued"]
    assert [record["args"]["text"] for record in records()] == ["one", "two"]
    assert outage.texts("bob") == []
    report = outbox.flush(InboxStore(outage.redis))
    assert len(report.applied) == 2
    assert outage.texts("bob") == ["one", "two"]


def test_a_corrupt_record_is_reported_and_skipped_never_replayed_twice(outage, capsys):
    outage.down = True
    for text in ("before", "broken", "after"):
        assert run("send", "bob", text) == 0
    lines = outbox.path().read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["args"]["text"] = "rewritten"
    outbox.path().write_text("\n".join([lines[0], json.dumps(tampered), "{not json", lines[2]]) + "\n")
    capsys.readouterr()

    outage.down = False
    report = outbox.flush(InboxStore(outage.redis))
    assert (report.corrupt, len(report.applied)) == (2, 2)
    assert capsys.readouterr().err.count("a corrupt outbox record was skipped") == 2
    assert outage.texts("bob") == ["before", "after"]
    assert records() == []
    assert [json.loads(line) for line in outbox.skipped_path().read_text().splitlines()] == [
        {"reason": "corrupt", "line": json.dumps(tampered) + "\n"},
        {"reason": "corrupt", "line": "{not json\n"},
    ]

    again = outbox.flush(InboxStore(outage.redis))
    assert (again.corrupt, again.applied) == (0, [])
    assert capsys.readouterr().err == ""
    assert outage.texts("bob") == ["before", "after"]


def test_a_record_redis_refuses_on_replay_is_reported_and_set_aside(outage, capsys):
    outage.down = True
    assert run("close", "nosuchitem", "cancel") == 0
    assert run("send", "bob", "still arrives") == 0
    refused = outbox.path().read_text().splitlines(keepends=True)[0]
    capsys.readouterr()
    outage.down = False
    report = outbox.flush(InboxStore(outage.redis))
    assert [reason for _, reason in report.rejected] == ["no message nosuchitem"]
    assert "no message nosuchitem" in capsys.readouterr().err
    assert outage.texts("bob") == ["still arrives"]
    assert records() == []
    assert json.loads(outbox.skipped_path().read_text()) == {"reason": "no message nosuchitem", "line": refused}


def test_replies_and_closes_made_while_redis_is_down_replay_once(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    told = store.send("alice", "bob", "the build is green", fyi=True)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    outage.down = True
    assert run("reply", asked.id, "on it") == 0
    assert run("close", told.id, "done") == 0
    queued = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["op"], row["state"]) for row in queued] == [("reply", "queued"), ("close", "queued")]

    outage.down = False
    assert run("inbox") == 0
    answer = queued[0]["id"]
    assert [(item.id, item.text, item.sender) for item in store.inbox("alice")] == [(answer, "on it", "bob")]
    assert (store.get(asked.id).state, store.get(asked.id).reason) == ("done", f"done: replied with message {answer}")
    assert store.get(told.id).state == "done"
    assert outbox.flush(store).applied == []
    assert len(store.inbox("alice")) == 1


def test_a_reply_whose_close_failed_mid_call_replays_only_the_close(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    close = InboxStore.close
    monkeypatch.setattr(InboxStore, "close", lambda *args: (_ for _ in ()).throw(lost("lost")))
    assert run("reply", asked.id, "on it") == 0
    answer = json.loads(capsys.readouterr().out)["id"]
    assert [item.id for item in store.inbox("alice")] == [answer]
    assert store.get(asked.id).state == "pending"
    monkeypatch.setattr(InboxStore, "close", close)
    report = outbox.flush(store)
    assert (report.applied, report.rejected) == ([answer], [])
    assert [item.id for item in store.inbox("alice")] == [answer]
    assert store.get(asked.id).reason == f"done: replied with message {answer}"


@pytest.mark.parametrize("command", [("reply", "on it"), ("close", "done", "answered")])
def test_a_reply_or_close_that_lands_before_redis_fails_replays_as_already_applied(
    outage, monkeypatch, capsys, command
):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    outage.failures = ["", "after"] if command[0] == "reply" else ["after"]
    assert run(command[0], asked.id, *command[1:]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "queued"
    history = store.history(asked.id)
    report = outbox.flush(store)
    assert (report.applied, report.rejected, len(report.already)) == ([], [], 1)
    assert store.history(asked.id) == history
    assert store.get(asked.id).state == "done"
    assert len(store.inbox("alice")) == (1 if command[0] == "reply" else 0)


def test_a_contended_replay_keeps_the_record_for_the_next_flush(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    outage.down = True
    assert run("close", asked.id, "done", "answered") == 0
    assert run("send", "alice", "after the close") == 0
    outage.down = False
    close = InboxStore.close
    monkeypatch.setattr(InboxStore, "close", lambda *args: (_ for _ in ()).throw(Contended("changed meanwhile")))
    report = outbox.flush(store)
    assert (report.applied, report.rejected, report.left) == ([], [], 2)
    assert len(records()) == 2
    monkeypatch.setattr(InboxStore, "close", close)
    assert len(outbox.flush(store).applied) == 2
    assert store.get(asked.id).reason == "done: answered"
    assert outage.texts("alice") == ["after the close"]


def test_a_fully_replayed_record_left_in_the_file_is_not_applied_again(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    told = store.send("alice", "bob", "please rebase")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    outage.down = True
    assert run("reply", asked.id, "on it") == 0
    assert run("close", told.id, "done", "rebased", "cleanly") == 0
    assert run("send", "alice", "rebased and pushed") == 0
    kept = outbox.path().read_text()
    outage.down = False
    assert len(outbox.flush(store).applied) == 3
    histories = [store.history(item.id) for item in (asked, told)]
    outbox.path().write_text(kept)
    report = outbox.flush(store)
    assert (report.applied, report.rejected) == ([], [])
    assert report.already == [json.loads(line)["item"] for line in kept.splitlines()]
    assert [item.text for item in store.inbox("alice")] == ["on it", "rebased and pushed"]
    assert [store.history(item.id) for item in (asked, told)] == histories
    assert store.get(told.id).reason == "done: rebased cleanly"


def test_a_send_with_a_given_id_refuses_a_second_write(outage):
    store = InboxStore(outage.redis)
    first = store.send("alice", "bob", "hello", item_id="fixedid")
    with pytest.raises(DuplicateItem, match="fixedid"):
        store.send("alice", "bob", "hello again", item_id="fixedid")
    assert [(item.id, item.text) for item in store.inbox("bob")] == [(first.id, "hello")]
    assert len(store.history("fixedid")) == 1


def test_a_full_outbox_refuses_loudly_and_keeps_the_oldest_record(outage, monkeypatch, capsys):
    outage.down = True
    assert run("send", "bob", "oldest") == 0
    size = outbox.path().stat().st_size
    monkeypatch.setenv(outbox.WARN_ENV, str(size + 1))
    monkeypatch.setenv(outbox.MAX_ENV, str(2 * size + 10))
    capsys.readouterr()
    assert run("send", "bob", "newer") == 0
    assert "outbox holds" in capsys.readouterr().err
    assert run("send", "bob", "refused") == 1
    err = capsys.readouterr().err
    assert "outbox is full" in err
    assert "this message was not queued and nothing queued was dropped: bring Redis back so the outbox replays" in err
    assert [record["args"]["text"] for record in records()] == ["oldest", "newer"]


def test_the_outbox_warning_stays_quiet_below_its_threshold(outage, monkeypatch, capsys):
    outage.down = True
    monkeypatch.setenv(outbox.WARN_ENV, "100000")
    assert run("send", "bob", "small") == 0
    assert "outbox holds" not in capsys.readouterr().err


def test_bounds_default_and_read_the_environment():
    assert outbox.bounds({}) == (outbox.DEFAULT_MAX_BYTES, outbox.DEFAULT_WARN_BYTES)
    assert outbox.bounds({outbox.MAX_ENV: "50", outbox.WARN_ENV: "40"}) == (50, 40)
    assert outbox.DEFAULT_WARN_BYTES < outbox.DEFAULT_MAX_BYTES


def test_the_outbox_lives_under_the_agentihooks_home():
    assert outbox.path().parent == config.AGENTIHOOKS_HOME
    assert outbox.skipped_path().parent == config.AGENTIHOOKS_HOME
    assert outbox.path() != outbox.skipped_path()


def test_a_record_is_checksummed_and_carries_its_item_id():
    record = outbox.record("send", "alice", {"address": "bob", "text": "hi", "fyi": False})
    assert outbox.parse(json.dumps(record)) == record
    assert len(record["item"]) == 12
    assert outbox.record("close", "bob", {"kind": "done"}, item="abc")["item"] == "abc"
    for key, value in (("by", "mallory"), ("op", "reply"), ("item", "other"), ("at", 1)):
        assert outbox.parse(json.dumps({**record, key: value})) is None
    assert outbox.parse(json.dumps({**record, "op": "drop", "sum": outbox.checksum({**record, "op": "drop"})})) is None
    assert outbox.parse(json.dumps([record])) is None
    assert outbox.parse("") is None


def test_commands_that_do_not_queue_still_refuse_without_redis(outage, capsys):
    outage.down = True
    assert run("inbox") == 1
    assert "unreachable" in capsys.readouterr().err
    assert records() == []


def test_a_refused_send_is_not_queued(outage, capsys):
    outage.down = True
    assert run("close", "someitem", "handoff") == 1
    assert "handoff needs" in capsys.readouterr().err
    assert run("close", "someitem", "finished") == 1
    assert records() == []


def test_the_inbox_hook_flushes_the_outbox_before_delivering(outage, monkeypatch):
    from hooks.context import inbox_delivery

    outage.down = True
    assert run("send", "bob", "while you were out") == 0
    outage.down = False
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    context = inbox_delivery.pending_context("sess-b")
    assert "while you were out" in context
    assert records() == []


def test_the_tick_flushes_the_outbox_first(outage, monkeypatch):
    from scripts.swarm import cli as swarm_cli
    from scripts.swarm.store import RedisStore

    outage.down = True
    assert run("send", "bob", "for the tick") == 0
    outage.down = False
    seen = []
    monkeypatch.setattr("scripts.swarm.lease.acquire", lambda *args: seen.append(outage.texts("bob")))
    assert swarm_cli.run_tick(RedisStore(outage.redis), "sw", ledger=object()) == ["the swarm belongs to another hive"]
    assert seen == [["for the tick"]]


def test_a_record_that_fails_unexpectedly_is_set_aside_and_the_rest_replays(outage, capsys):
    outage.down = True
    assert run("send", "bob", "after the poison") == 0
    poison = outbox.record("send", "alice", {"address": "bob"})
    outbox.path().write_text(json.dumps(poison) + "\n" + outbox.path().read_text())
    outage.down = False
    report = outbox.flush(InboxStore(outage.redis))
    assert ([item for item, _ in report.rejected], len(report.applied)) == ([poison["item"]], 1)
    assert outage.texts("bob") == ["after the poison"]
    assert records() == []


def test_a_queued_close_matching_another_actors_close_is_refused_not_taken_as_its_own(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    outage.down = True
    assert run("close", asked.id, "done", "answered") == 0
    store.close(asked.id, "alice", "done", "answered")
    outage.down = False
    report = outbox.flush(store)
    assert (report.already, report.rejected) == ([], [(asked.id, f"message {asked.id} is closed: done: answered")])


def test_an_unexpected_replay_error_stops_the_flush_and_keeps_the_record(outage, monkeypatch, capsys):
    outage.down = True
    assert run("send", "bob", "kept") == 0
    outage.down = False
    capsys.readouterr()
    monkeypatch.setitem(cli.APPLY, "send", lambda *args: (_ for _ in ()).throw(RuntimeError("disk gone")))
    report = outbox.flush(InboxStore(outage.redis))
    assert (report.applied, report.rejected, report.left) == ([], [], 1)
    err = capsys.readouterr().err
    assert "outbox replay stopped (RuntimeError: disk gone)" in err and "1 queued messages wait" in err
    assert [record["args"]["text"] for record in records()] == ["kept"]


def test_a_live_close_that_meets_contention_is_queued_and_replays(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    close = InboxStore.close
    monkeypatch.setattr(InboxStore, "close", lambda *args: (_ for _ in ()).throw(Contended("changed meanwhile")))
    assert run("close", asked.id, "done", "answered") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "queued"
    monkeypatch.setattr(InboxStore, "close", close)
    assert outbox.flush(store).applied == [asked.id]
    assert store.get(asked.id).reason == "done: answered"


def test_the_outbox_bounds_take_an_exact_fit_and_warn_at_the_threshold(outage, monkeypatch, capsys):
    entry = outbox.record("send", "alice", {"address": "bob", "text": "fits", "fyi": False})
    size = len(json.dumps(entry)) + 1
    monkeypatch.setenv(outbox.MAX_ENV, str(size))
    monkeypatch.setenv(outbox.WARN_ENV, str(size))
    outbox.append(entry)
    assert f"holds {size} bytes of its {size} byte limit" in capsys.readouterr().err
    with pytest.raises(outbox.OutboxFull, match=rf"full \({size} of {size} bytes"):
        outbox.append(outbox.record("send", "alice", {"address": "bob", "text": "over", "fyi": False}))
    assert records() == [entry]


def test_a_flush_skips_while_another_process_holds_the_outbox(outage, monkeypatch):
    outage.down = True
    assert run("send", "bob", "waits") == 0
    outage.down = False
    asked = []

    @contextmanager
    def held(blocking=True):
        asked.append(blocking)
        yield False

    monkeypatch.setattr(outbox, "locked", held)
    report = outbox.flush(InboxStore(outage.redis))
    assert (asked, report.applied, len(records())) == ([False], [], 1)


def test_a_live_reply_and_a_repeated_close_report_what_happened(outage, monkeypatch, capsys):
    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    told = store.send("alice", "bob", "please rebase")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("reply", asked.id, "on it") == 0
    answer = store.inbox("alice")[0].id
    assert json.loads(capsys.readouterr().out) == {"id": answer, "to": "alice", "closed": asked.id}
    store.read(told.id, "bob")
    assert run("close", told.id, "done", "rebased", "cleanly") == 0
    capsys.readouterr()
    assert run("close", told.id, "done", "rebased", "cleanly") == 1
    assert f"message {told.id} is already closed: done: rebased cleanly" in capsys.readouterr().err


def test_store_replies_get_fresh_answers_and_refuse_a_replayed_answer(outage):
    store = InboxStore(outage.redis)
    one, two = store.send("alice", "bob", "one"), store.send("alice", "bob", "two")
    first, second = store.reply(one.id, "bob", "first"), store.reply(two.id, "bob", "second")
    assert first.id != second.id and not first.fyi
    with pytest.raises(DuplicateItem, match=f"message {one.id} was already answered with message {first.id}"):
        store.reply(one.id, "bob", "first", answer_id=first.id)


def test_a_close_that_keeps_meeting_contention_raises_contended(outage, monkeypatch):
    from redis.exceptions import WatchError

    store = InboxStore(outage.redis)
    asked = store.send("alice", "bob", "can you review")
    monkeypatch.setattr(InboxStore, "_try_move", lambda *args: (_ for _ in ()).throw(WatchError()))
    with pytest.raises(Contended, match=f"message {asked.id} changed meanwhile; run the command again"):
        store.close(asked.id, "bob", "done", "answered")


def test_connect_reaches_redis_with_the_given_environment(outage):
    from scripts.inbox.store import connect

    environ = {"REDIS_URL": "redis://elsewhere:6379"}
    connect(environ)
    assert outage.environs[-1] is environ


def test_a_kept_tail_is_staged_beside_the_outbox_so_its_replace_is_atomic(outage, monkeypatch):
    outage.down = True
    assert run("send", "bob", "kept") == 0
    staged = []
    mkstemp = outbox.tempfile.mkstemp
    monkeypatch.setattr(outbox.tempfile, "mkstemp", lambda **kwargs: staged.append(kwargs) or mkstemp(**kwargs))
    outage.failures = ["before"]
    outage.down = False
    assert outbox.flush(InboxStore(outage.redis)).left == 1
    assert staged == [{"dir": outbox.home()}]
    assert list(outbox.home().glob("tmp*")) == []


def poisoned(monkeypatch, failing):
    send = cli.APPLY["send"]

    def apply(store, me, record):
        if record["args"]["text"] in failing:
            raise RuntimeError("disk gone")
        return send(store, me, record)

    monkeypatch.setitem(cli.APPLY, "send", apply)


def test_a_record_that_keeps_failing_unexpectedly_is_set_aside_after_its_attempts_and_the_rest_replays(
    outage, monkeypatch, capsys
):
    outage.down = True
    assert run("send", "bob", "poison") == 0
    assert run("send", "bob", "behind") == 0
    poison = outbox.path().read_text().splitlines(keepends=True)[0]
    item = json.loads(poison)["item"]
    outage.down = False
    poisoned(monkeypatch, {"poison"})
    capsys.readouterr()
    for attempt in (1, 2):
        report = outbox.flush(InboxStore(outage.redis))
        assert (report.applied, report.rejected, report.left) == ([], [], 2)
        assert capsys.readouterr().err == (
            "agentihooks msg: outbox replay stopped (RuntimeError: disk gone)\n"
            f"agentihooks msg: 2 queued messages wait in {outbox.path()} for Redis or the ledger server\n"
        )
        assert json.loads(outbox.attempts_path().read_text()) == {json.loads(poison)["sum"]: attempt}
        assert outbox.status() == {"queued": 2, "retrying": 1, "set_aside": 0}
        assert outbox.status_lines() == ["inbox outbox  queued 2  retrying 1  set aside 0"]
    report = outbox.flush(InboxStore(outage.redis))
    reason = "RuntimeError: disk gone on 3 replays (AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS)"
    assert (report.rejected, len(report.applied), report.left) == ([(item, reason)], 1, 0)
    assert capsys.readouterr().err == (
        f"agentihooks msg: queued message {item} was refused on replay ({reason}); kept in {outbox.skipped_path()}\n"
    )
    assert outage.texts("bob") == ["behind"]
    assert records() == []
    assert json.loads(outbox.skipped_path().read_text()) == {"reason": reason, "line": poison}
    assert outbox.status() == {"queued": 0, "retrying": 0, "set_aside": 1}
    assert outbox.status_lines() == ["inbox outbox  queued 0  retrying 0  set aside 1"]
    assert not outbox.attempts_path().exists()


def test_a_record_that_replays_after_an_unexpected_failure_forgets_its_attempts(outage, monkeypatch):
    outage.down = True
    assert run("send", "bob", "flaky") == 0
    assert run("send", "bob", "steady") == 0
    outage.down = False
    failing = {"flaky"}
    poisoned(monkeypatch, failing)
    outbox.flush(InboxStore(outage.redis))
    assert json.loads(outbox.attempts_path().read_text()) == {records()[0]["sum"]: 1}
    failing.clear()
    report = outbox.flush(InboxStore(outage.redis))
    assert (len(report.applied), report.rejected, report.left) == (2, [], 0)
    assert outage.texts("bob") == ["flaky", "steady"]
    assert not outbox.attempts_path().exists()
    assert outbox.status() == {"queued": 0, "retrying": 0, "set_aside": 0}
    assert outbox.status_lines() == []


def test_the_attempt_cap_defaults_and_reads_the_environment(monkeypatch):
    assert outbox.max_attempts({}) == 3
    assert outbox.max_attempts({"AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS": ""}) == 3
    assert outbox.max_attempts({"AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS": "5"}) == 5
    monkeypatch.setenv("AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS", "1")
    assert outbox.max_attempts() == 1
    assert (outbox.attempts_path().parent, outbox.attempts_path().name) == (
        config.AGENTIHOOKS_HOME,
        "inbox-outbox.attempts.json",
    )


def test_a_cap_of_one_sets_aside_on_the_first_unexpected_failure(outage, monkeypatch):
    outage.down = True
    assert run("send", "bob", "poison") == 0
    outage.down = False
    poisoned(monkeypatch, {"poison"})
    monkeypatch.setenv("AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS", "1")
    report = outbox.flush(InboxStore(outage.redis))
    assert [reason for _, reason in report.rejected] == [
        "RuntimeError: disk gone on 1 replays (AGENTIHOOKS_INBOX_OUTBOX_MAX_ATTEMPTS)"
    ]
    assert records() == []
    assert not outbox.attempts_path().exists()
