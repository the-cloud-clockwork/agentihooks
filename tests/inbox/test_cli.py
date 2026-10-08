import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "registered_name", lambda: "", raising=False)
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions",
        lambda **kwargs: {"sess-a": {"name": "alice"}, "sess-b": {"name": "bob"}},
    )
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "alice")
    return store


def run(*argv):
    return cli.main(list(argv))


def test_send_then_inbox_shows_a_pending_item_from_the_session(store, monkeypatch, capsys):
    assert run("send", "bob", "review", "my", "branch") == 0
    sent = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("inbox") == 0
    assert capsys.readouterr().out.split("\t")[:3] == [sent["id"], "pending", "alice"]
    assert store.get(sent["id"]).text == "review my branch"
    assert store.get(sent["id"]).task == ""


@pytest.mark.parametrize("address", ["sw-eng-1", "eng-1@sw"])
def test_send_records_the_receivers_current_task(store, capsys, monkeypatch, address):
    from scripts.swarm.store import AgentRecord, RedisStore

    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions",
        lambda **kwargs: {"sess-a": {"name": "alice"}, "sess-b": {"name": "sw-eng-1"}},
    )
    swarm = RedisStore(store.redis)
    swarm.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw"))
    swarm.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    assert run("send", address, "Tell me when your branch is pushed.") == 0
    sent = json.loads(capsys.readouterr().out)
    assert store.get(sent["id"]).task == "t1"


@pytest.mark.parametrize("state, task", [("finished", "t1"), ("working", "master")])
def test_a_receiver_without_a_live_worker_task_has_no_task_association(store, state, task):
    from scripts.swarm.store import AgentRecord, RedisStore

    swarm = RedisStore(store.redis)
    swarm.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task=task, state=state))
    swarm.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2"))
    assert store.receiver_task("sw-eng-1") == ""
    assert store.receiver_task("eng-1@sw") == ""
    assert store.receiver_task("sw-eng-9") == ""


def test_a_receivers_alias_records_the_current_task(store):
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    swarm = RedisStore(store.redis)
    swarm.create(SwarmConfig("sw", "/repo", 2, 1))
    name = swarm.next_name("sw", "eng")
    swarm.put_agent("sw", AgentRecord(name=name, lane="eng", task="t1"))
    store.names.alias("old-receiver", name)
    assert store.receiver_task("old-receiver") == "t1"


def test_task_lookup_preserves_the_whole_swarm_name_in_a_seat_address(store):
    from scripts.swarm.store import AgentRecord, RedisStore

    swarm = RedisStore(store.redis)
    swarm.put_agent("sw@part", AgentRecord(name="sw-eng-1", lane="eng", task="t1"))
    store.seats.occupy("eng-1@sw@part", "sw-eng-1", 1)
    assert store.receiver_task("eng-1@sw@part") == "t1"


def test_a_reply_to_the_operator_with_a_clock_time_is_refused_at_send(store, capsys):
    asked = store.send("operator", "alice", "is the fix merged")
    assert run("reply", asked.id, "merged", "at", "18:45") == 1
    assert "clock time '18:45'" in capsys.readouterr().err
    assert store.get(asked.id).state == "pending" and store.inbox("operator") == []
    assert run("reply", asked.id, "merged", "and", "deployed") == 0
    assert [i.text for i in store.inbox("operator")] == ["merged and deployed"]


def test_a_reply_to_the_operator_accepts_an_issue_link(store, capsys):
    asked = store.send("operator", "alice", "where is the issue")
    text = "The issue is https://github.com/the-cloud-clockwork/agentihooks/issues/613"
    assert run("reply", asked.id, text) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["closed"] == asked.id
    assert [i.text for i in store.inbox("operator")] == [text]
    assert store.get(asked.id).state == "done"


def test_a_message_sent_to_the_operator_with_a_clock_time_is_refused_at_send(store, capsys):
    assert run("send", "operator", "done", "at", "18:45") == 1
    assert "clock time" in capsys.readouterr().err and store.inbox("operator") == []


@pytest.mark.parametrize(
    "argv",
    [
        ("send", "bob", "hi", "--from", "mallory"),
        ("send", "bob", "--as", "mallory", "hi"),
        ("send", "bob", "--sender=mallory", "hi"),
    ],
)
def test_a_forged_sender_argument_is_ignored(store, argv):
    assert run(*argv) == 0
    [item] = store.inbox("bob")
    assert item.sender == "alice"
    assert item.text == " ".join(argv[2:])


def test_a_sender_option_before_the_address_sends_nothing(store):
    with pytest.raises(SystemExit):
        run("send", "--from", "mallory", "bob", "hi")
    assert store.inbox("bob") == []


def test_the_session_id_is_the_sender_without_an_agent_name(store, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    run("send", "bob", "hi")
    assert store.inbox("bob")[0].sender == "sess-1"


def test_a_session_named_on_its_record_speaks_under_that_name(store, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setattr(cli, "registered_name", lambda: "sw-master-1")
    run("send", "bob", "hi")
    assert store.inbox("bob")[0].sender == "sw-master-1"


def test_a_session_without_identity_is_refused(store, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    assert run("send", "bob", "hi") == 1
    assert "identity" in capsys.readouterr().err
    assert store.inbox("bob") == []


def test_read_shows_the_item_and_marks_it_read(store, monkeypatch, capsys):
    item = store.send("alice", "bob", "hi")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("read", item.id) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["text"], shown["state"]) == ("hi", "read")
    assert [h["state"] for h in shown["history"]] == ["pending", "read"]


def test_closing_without_a_reason_is_refused(store, monkeypatch, capsys):
    item = store.send("alice", "bob", "hi")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("close", item.id) == 1
    assert "reason" in capsys.readouterr().err
    assert store.get(item.id).state == "pending"


def test_close_with_a_handoff_names_the_address(store, monkeypatch, capsys):
    item = store.send("alice", "bob", "hi")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("close", item.id, "handoff", "carol") == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "handed off to carol"
    assert [h["state"] for h in store.history(item.id)] == ["pending", "handed_off"]


def test_another_sessions_item_is_refused(store, capsys):
    item = store.send("bob", "carol", "hi")
    assert run("read", item.id) == 1
    assert "belongs to carol" in capsys.readouterr().err


def test_reply_answers_the_sender_and_closes_the_item(store, monkeypatch, capsys):
    item = store.send("bob", "alice", "is the branch ready")
    assert run("reply", item.id, "yes,", "pushed") == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["to"], out["closed"]) == ("bob", item.id)
    [answer] = store.inbox("bob")
    assert (answer.sender, answer.text) == ("alice", "yes, pushed")
    assert store.get(item.id).state == "done"


def test_a_reply_to_a_swarm_notice_closes_it_without_sending_a_reply(store, capsys):
    notice = store.send("swarm", "alice", "checks passed on your pull request")
    assert run("reply", notice.id, "thanks") == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"id": notice.id, "state": "done", "reason": "done: thanks"}
    assert out.err == ""
    assert store.get(notice.id).state == "done"
    assert store.inbox("swarm") == []


@pytest.mark.parametrize("words", [["Fixed checks and pushed"], ["Fixed checks and pushed", "--fyi"]])
def test_reply_to_tick_preserves_the_supplied_outcome(store, words):
    notice = store.send("swarm", "alice", "Fix red checks and push")
    assert run("reply", notice.id, *words) == 0
    assert store.get(notice.id).reason == "done: Fixed checks and pushed"
    assert store.inbox("swarm") == []


def test_empty_reply_to_tick_refuses_done_without_mutation(store):
    notice = store.send("swarm", "alice", "Fix red checks and push")
    history = store.history(notice.id)
    assert run("reply", notice.id, "  ") == 1
    assert store.get(notice.id) == notice
    assert store.history(notice.id) == history


def test_send_and_reply_with_fyi_mark_the_item_as_needing_no_work(store, capsys):
    assert run("send", "bob", "--fyi", "thanks,", "merged") == 0
    [thanks] = store.inbox("bob")
    assert (thanks.text, thanks.fyi) == ("thanks, merged", True)
    item = store.send("bob", "alice", "is the branch ready")
    assert run("reply", item.id, "--fyi", "yes") == 0
    assert run("send", "bob", "review", "my", "branch") == 0
    answer, plain = store.inbox("bob")[1:]
    assert (answer.text, answer.fyi) == ("yes", True)
    assert (plain.text, plain.fyi) == ("review my branch", False)


@pytest.mark.parametrize("words", [["--fyi", "Confirmed"], ["Confirmed", "--fyi"], ["--fyi", "Confirmed", "--fyi"]])
def test_information_flag_at_either_end_is_parsed_on_send_and_reply(store, words):
    assert run("send", "bob", *words) == 0
    [sent] = store.inbox("bob")
    assert (sent.text, sent.fyi) == ("Confirmed", True)
    question = store.send("bob", "alice", "Ready?")
    assert run("reply", question.id, *words) == 0
    answer = store.inbox("bob")[-1]
    assert (answer.text, answer.fyi) == ("Confirmed", True)


def test_information_flag_inside_quoted_text_remains_text(store):
    assert run("send", "bob", "Please explain --fyi") == 0
    [sent] = store.inbox("bob")
    assert (sent.text, sent.fyi) == ("Please explain --fyi", False)


def test_information_parser_returns_false_for_work_text():
    assert cli.informational(["Fix", "checks"]) == (False, ["Fix", "checks"])


def test_trailing_information_flag_preserves_all_unquoted_words_on_send_and_reply(store):
    assert run("send", "bob", "Confirmed", "merged", "--fyi") == 0
    [sent] = store.inbox("bob")
    assert (sent.text, sent.fyi) == ("Confirmed merged", True)
    question = store.send("bob", "alice", "Ready?")
    assert run("reply", question.id, "Confirmed", "merged", "--fyi") == 0
    answer = store.inbox("bob")[-1]
    assert (answer.text, answer.fyi) == ("Confirmed merged", True)


def test_multi_agent_chat_rooms_are_retired_without_dangling_references():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert not (root / "profiles" / "package" / "skills" / "multi-agent-chat").exists()
    tracked = subprocess.run(
        ["git", "grep", "-l", "multi-agent-chat"], cwd=root, capture_output=True, text=True
    ).stdout.split()
    assert set(tracked) <= {"CHANGELOG.md", "profiles/_base/lifecycle.json", "tests/inbox/test_cli.py"}
