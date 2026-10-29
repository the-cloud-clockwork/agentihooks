import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.inbox import seen  # noqa: E402
from scripts.inbox.store import InboxStore  # noqa: E402
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig  # noqa: E402
from scripts.swarm_ledger import ledger_server  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "relay-outage-2026-01-01"
ENG = AgentRecord("ro-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
BOSS = AgentRecord("ro-master-1", MASTER, MASTER, pane_id="pm", seat=f"master@{SLUG}")
LONG = " ".join(["word"] * 120)


class Outage:
    """One fake Redis server the ledger server connects to, refused while it is down."""

    def __init__(self, monkeypatch):
        import fakeredis

        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        store = RedisStore(self.redis)
        store.create(SwarmConfig(SLUG, "/repo", 1, 0))
        for agent in (ENG, BOSS):
            store.put_agent(SLUG, agent)
            store.seats.occupy(agent.seat, agent.name, 1)
        monkeypatch.setattr("scripts.swarm.store.redis_client", self.client)
        monkeypatch.setattr(seen, "marks_for", lambda slug, environ=None: seen.SeenMarks(self.redis))

    def client(self, environ=None):
        import fakeredis

        client = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        client.ping()
        return client

    def down(self, value=True):
        self.server.connected = not value

    def texts(self, address):
        return [item.text for item in InboxStore(self.redis).inbox(address)]


@pytest.fixture
def outage(monkeypatch):
    return Outage(monkeypatch)


def make_ledger(description="d"):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": description}],
        "questions": [{"text": "which broker?"}],
    }
    html_path, json_path = legacy_page.paths(SLUG, core)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    return state["phases"][0]["id"], f"questions/{state['questions'][0]['id']}"


def write(op, refused=()):
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == list(refused)
    ledger_server.relay_to_inbox(SLUG, state)
    ledger_server.deliver_alerts(SLUG, state)
    return state


def operator_writes(phase, question):
    write({"op": "add", "thread": "chat", "id": "m-1", "text": "@ro-eng-1 please rebase onto dev"})
    write({"op": "add", "thread": f"phases/{phase}/comments", "id": "c-1", "text": "the phase needs a doc"})
    write({"op": "add", "thread": f"{question}/answers", "id": "an-1", "text": "use the queue"})


def mentions(texts, words):
    return [text for text in texts if words in text]


def assert_each_write_reached_its_agent_once(outage):
    eng, boss = outage.texts(ENG.seat), outage.texts(BOSS.seat)
    assert len(mentions(eng, "please rebase onto dev")) == 1
    assert len(mentions(boss, "the phase needs a doc")) == 1
    assert len(mentions(boss, "use the queue")) == 1
    assert mentions(boss, "please rebase onto dev") == [] and mentions(eng, "use the queue") == []


def test_writes_made_while_redis_is_down_reach_their_agents_once_after_recovery(outage, monkeypatch):
    phase, question = make_ledger()
    outage.down()
    operator_writes(phase, question)
    swarm_say(monkeypatch)
    outage.down(False)
    ledger_server.replay_unrelayed()
    ledger_server.replay_unrelayed()
    replay_outbox(outage)
    assert_each_write_reached_its_agent_once(outage)
    assert len(mentions(outage.texts(ENG.name), "the docs build is green")) == 1
    assert ledger_server.unrelayed() == {}


def swarm_say(monkeypatch):
    from scripts.swarm import cli

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", BOSS.name)
    assert cli.main([SLUG, "say", "the docs build is green", "--to", "eng"]) == 0


def replay_outbox(outage):
    from scripts.inbox import outbox

    outbox.flush(InboxStore(outage.redis))
    outbox.flush(InboxStore(outage.redis))
    assert not outbox.waiting()


def test_a_swarm_say_whose_recipient_lookup_loses_redis_reaches_its_agent_once_after_recovery(outage, monkeypatch):
    from scripts.swarm import cli

    monkeypatch.setattr(cli, "connect", lambda: RedisStore(outage.redis))
    outage.down()
    swarm_say(monkeypatch)
    outage.down(False)
    replay_outbox(outage)
    assert len(mentions(outage.texts(ENG.name), "the docs build is green")) == 1


def test_the_next_write_after_recovery_replays_every_revision_it_could_not_relay(outage):
    phase, question = make_ledger()
    outage.down()
    operator_writes(phase, question)
    outage.down(False)
    write({"op": "add", "thread": "chat", "id": "m-2", "text": "@ro-eng-1 and run the docs build"})
    ledger_server.replay_unrelayed()
    assert_each_write_reached_its_agent_once(outage)
    assert len(mentions(outage.texts(ENG.seat), "run the docs build")) == 1


def test_a_write_relayed_while_redis_is_up_leaves_nothing_to_replay(outage):
    phase, question = make_ledger()
    operator_writes(phase, question)
    assert ledger_server.unrelayed() == {}
    ledger_server.replay_unrelayed()
    assert_each_write_reached_its_agent_once(outage)


def test_an_alert_raised_while_redis_is_down_reaches_the_master_once_after_recovery(outage):
    make_ledger()
    outage.down()
    taken = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    write(taken, refused=["plan"])
    outage.down(False)
    ledger_server.replay_unrelayed()
    ledger_server.replay_unrelayed()
    assert len([text for text in outage.texts(BOSS.seat) if "alert" in text]) == 1
    assert ledger_server.unrelayed() == {}


@pytest.mark.parametrize("kind", [ledger_server.RELAY, ledger_server.ALERTS])
def test_a_held_revision_with_nothing_left_to_send_clears_on_replay(outage, kind):
    make_ledger()
    state, _ = core.sync(SLUG)
    ledger_server.unrelayed_on(kind, SLUG, state["_meta"]["rev"])
    ledger_server.replay_unrelayed()
    assert ledger_server.unrelayed() == {}


def test_a_held_revision_of_a_ledger_that_is_gone_is_dropped_and_the_next_ledger_still_replays(outage):
    make_ledger()
    state, _ = core.sync(SLUG)
    ledger_server.unrelayed_on(ledger_server.RELAY, "gone-2026-01-01", 3)
    ledger_server.unrelayed_on(ledger_server.RELAY, SLUG, state["_meta"]["rev"])
    ledger_server.replay_unrelayed()
    assert ledger_server.unrelayed() == {}


def test_an_alert_held_during_the_outage_goes_out_with_the_next_write_after_recovery(outage):
    make_ledger()
    outage.down()
    taken = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    write(taken, refused=["plan"])
    outage.down(False)
    write({"op": "add", "thread": "chat", "id": "m-3", "text": "back again"})
    assert len([text for text in outage.texts(BOSS.seat) if "alert" in text]) == 1
    assert ledger_server.unrelayed() == {}


def test_a_relay_forgets_only_the_revisions_it_covered():
    for rev in (3, 9):
        ledger_server.unrelayed_on(ledger_server.RELAY, SLUG, rev)
    ledger_server.relayed(ledger_server.RELAY, SLUG, 1, 5)
    assert ledger_server.unrelayed() == {ledger_server.RELAY: {SLUG: [9]}}


def first_read(monkeypatch, seen):
    """unrelayed() answers seen once, as another thread's write lands, then reads the file again."""
    real, calls = ledger_server.unrelayed, []

    def read():
        calls.append(1)
        return seen if len(calls) == 1 else real()

    monkeypatch.setattr(ledger_server, "unrelayed", read)
    return real


@pytest.mark.parametrize("left", [{}, {ledger_server.RELAY: {"other-2026-01-01": [7]}}])
def test_a_relay_whose_revisions_another_thread_forgot_leaves_the_record_as_it_found_it(monkeypatch, left):
    for slug, revs in left.get(ledger_server.RELAY, {}).items():
        ledger_server.unrelayed_on(ledger_server.RELAY, slug, revs[0])
    real = first_read(monkeypatch, {ledger_server.RELAY: {SLUG: [5]}})
    ledger_server.relayed(ledger_server.RELAY, SLUG, 0, 9)
    assert real() == left


def test_a_replay_whose_gone_ledger_another_thread_forgot_carries_on(monkeypatch):
    real = first_read(monkeypatch, {ledger_server.RELAY: {"gone-2026-01-01": [4]}})
    ledger_server.replay_unrelayed()
    assert real() == {}


def test_the_unrelayed_record_creates_its_folder(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", str(tmp_path / "home" / "state"))
    ledger_server.unrelayed_on(ledger_server.RELAY, SLUG, 3)
    assert ledger_server.unrelayed() == {ledger_server.RELAY: {SLUG: [3]}}


def test_a_trimmed_range_notice_names_the_range_and_resumes_the_relay_after_it(outage):
    make_ledger()
    ledger_server.unrelayed_on(ledger_server.RELAY, SLUG, 3)
    assert ledger_server.record_trimmed(SLUG, 3, 7) == 8
    state, _ = core.sync(SLUG)
    [notice] = [row for row in state["notifications"] if "left the ledger history" in row["text"]]
    assert "from revision 3 through 7" in notice["text"] and notice["by"] == "ledger"
    assert ledger_server.unrelayed() == {}


class Unwritable:
    def apply_ops(self, slug, **kwargs):
        raise OSError("disk full")


def test_a_trimmed_range_whose_notice_cannot_be_written_stays_held(outage, monkeypatch, capsys):
    ledger_server.unrelayed_on(ledger_server.RELAY, SLUG, 3)
    monkeypatch.setattr(ledger_server, "repository", Unwritable())
    assert ledger_server.record_trimmed(SLUG, 3, 7) == 3
    assert "trimmed notice for" in capsys.readouterr().err
    assert ledger_server.unrelayed() == {ledger_server.RELAY: {SLUG: [3]}}


class Stop(Exception):
    pass


def watch_passes(monkeypatch, held, replay, count=6):
    """Run the watch loop for count passes with everything but the unrelayed replay stubbed out."""
    passes = []

    def sleep(_):
        passes.append(1)
        if len(passes) == count:
            raise Stop

    for name in ("bin_closed_without_swarm", "expire_alerts", "sample_streams"):
        monkeypatch.setattr(ledger_server, name, lambda: None)
    monkeypatch.setattr(ledger_server.ledger_bin, "tidy", lambda: None)
    monkeypatch.setattr(ledger_server, "reloading", lambda *args: False)
    monkeypatch.setattr(ledger_server, "unrelayed", lambda: held)
    monkeypatch.setattr(ledger_server, "replay_unrelayed", lambda: replay(len(passes)))
    monkeypatch.setattr(ledger_server, "scheduled_wait", sleep)
    with pytest.raises(Stop):
        ledger_server.watch_ledgers()


def test_the_watch_loop_replays_held_revisions_every_fifth_pass_and_survives_a_failed_replay(
    monkeypatch, tmp_path, capsys
):
    held, replayed = {ledger_server.RELAY: {SLUG: [1]}}, []

    def replay(at):
        replayed.append(at)
        raise OSError("disk full")

    watch_passes(monkeypatch, held, replay)
    assert replayed == [0, 5] and "unrelayed replay: disk full" in capsys.readouterr().err


def test_the_watch_loop_never_replays_when_nothing_is_held(monkeypatch, tmp_path):
    replayed = []
    watch_passes(monkeypatch, {}, replayed.append)
    assert replayed == []


class Unreadable:
    def held_revisions(self):
        raise sqlite3.OperationalError("database is locked")


def test_an_unreadable_relay_journal_reads_as_nothing_held(monkeypatch):
    monkeypatch.setattr(ledger_server, "stored", Unreadable())
    assert ledger_server.unrelayed() == {}


def test_the_watch_loop_survives_a_failed_journal_read(monkeypatch, capsys):
    replayed = []

    def unreadable():
        raise OSError("disk gone")

    monkeypatch.setattr(ledger_server, "unrelayed", unreadable)
    passes = []
    monkeypatch.setattr(ledger_server, "scheduled_wait", lambda _: passes.append(1) or len(passes) == 6 and stop())
    for name in ("bin_closed_without_swarm", "expire_alerts", "sample_streams"):
        monkeypatch.setattr(ledger_server, name, lambda: None)
    monkeypatch.setattr(ledger_server.ledger_bin, "tidy", lambda: None)
    monkeypatch.setattr(ledger_server, "reloading", lambda *args: False)
    monkeypatch.setattr(ledger_server, "replay_unrelayed", lambda: replayed.append(1))
    with pytest.raises(Stop):
        ledger_server.watch_ledgers()
    assert replayed == [] and capsys.readouterr().err.count("unrelayed replay: disk gone") == 2


def stop():
    raise Stop


def test_each_operator_write_trimmed_before_its_relay_leaves_one_notice_for_the_operator(outage, monkeypatch):
    phase, question = make_ledger()
    monkeypatch.setattr(core, "EVENTS_KEPT", 1)
    monkeypatch.setattr(core, "EVENTS_CEILING", 1)
    outage.down()
    operator_writes(phase, question)
    outage.down(False)
    ledger_server.replay_unrelayed()
    ledger_server.replay_unrelayed()
    state, _ = core.sync(SLUG)
    notices = [row["text"] for row in state.get("notifications", []) if "left the ledger history" in row["text"]]
    assert len(notices) == len(set(notices)) == 2 and ledger_server.unrelayed() == {}
    assert len(mentions(outage.texts(BOSS.seat), "use the queue")) == 1


def test_a_journal_an_older_server_left_in_its_own_file_is_held_once_and_the_file_removed(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", str(tmp_path))
    legacy = tmp_path / ledger_server.UNRELAYED
    legacy.write_text('{"relay": {"%s": [3, 5]}, "alerts": {"%s": [4]}}' % (SLUG, SLUG))
    ledger_server.adopt_unrelayed()
    assert not legacy.exists()
    assert ledger_server.unrelayed() == {ledger_server.RELAY: {SLUG: [3, 5]}, ledger_server.ALERTS: {SLUG: [4]}}
    ledger_server.adopt_unrelayed()
    assert ledger_server.unrelayed() == {ledger_server.RELAY: {SLUG: [3, 5]}, ledger_server.ALERTS: {SLUG: [4]}}


@pytest.mark.parametrize(
    "text", ["not json", "[3]", '{"relay": []}', '{"relay": {"s": "x"}}', '{"relay": {"s": ["x"]}}']
)
def test_an_unreadable_or_foreign_journal_file_holds_nothing(monkeypatch, tmp_path, text):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", str(tmp_path))
    (tmp_path / ledger_server.UNRELAYED).write_text(text)
    ledger_server.adopt_unrelayed()
    assert ledger_server.unrelayed() == {}
