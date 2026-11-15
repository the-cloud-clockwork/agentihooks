import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO, cast
from unittest.mock import patch

import pytest

from scripts.hub import admission
from scripts.inbox.dispatch import Dispatcher
from scripts.inbox.store import InboxStore
from scripts.swarm import cli, commands, name_state, readiness, resume, snapshot
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import tick
from tests.helpers.seat import seat_agent
from tests.helpers.seat import view as seat_view
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm_ledger import legacy_page

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core as core  # noqa: E402


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def seed(store, slug):
    store.create(SwarmConfig(slug, "/repo", 2, 1, template="lean", lanes={"eng": {"model": "opus"}}))
    eng, master = store.next_name(slug, "eng"), store.next_name(slug, MASTER)
    store.put_agent(slug, AgentRecord(eng, "eng", "t1", pane_id="w1:p1", started_at=5, seat=f"eng-1@{slug}"))
    store.put_agent(slug, AgentRecord(master, MASTER, MASTER, pane_id="w1:m1", started_at=5, seat=f"master@{slug}"))
    store.claim(slug, "t1", eng, lease_ms=600_000)
    store.put_handoff(slug, "t2", "carry on from the parser", seat=f"eng-2@{slug}")
    store.seats.occupy(f"eng-1@{slug}", eng, 10)
    store.seats.occupy(f"master@{slug}", master, 10)
    store.memory.add_recap(f"eng-1@{slug}", eng, "t1", "parser half done", 20)
    store.memory.learn(f"eng-1@{slug}", eng, "run the gates twice", 21)
    inbox = InboxStore(store.redis)
    pending = inbox.send("operator", f"master@{slug}", "how far along is the parser")
    answered = inbox.send(master, eng, "open the pull request")
    inbox.reply(answered.id, eng, "opened")
    return pending


def everything(redis):
    out = {}
    for key in sorted(redis.scan_iter()):
        if f":state:{name_state.BUCKET}:" in key:
            continue
        kind = redis.type(key)
        read = {
            "string": redis.get,
            "hash": redis.hgetall,
            "list": lambda k: redis.lrange(k, 0, -1),
            "set": lambda k: sorted(redis.smembers(k)),
            "zset": lambda k: redis.zrange(k, 0, -1, withscores=True),
        }[kind]
        out[key] = (kind, read(key))
    return out


def registered_codes(store):
    codes = {slug: store.names.code_of(slug) for slug in ("sw", "other")}
    assert all(codes.values())
    return {slug: (code, store.names.swarm(code)) for slug, code in codes.items()}


def test_export_then_restore_after_losing_redis_gives_back_the_same_swarm_state(store):
    from scripts.swarm import fencing

    seed(store, "sw")
    fencing.read(store, "sw")
    alone, names = everything(store.redis), store.names.export("sw")
    seed(store, "other")
    state = store.export("sw")
    store.redis.flushall()
    store.restore("sw", json.loads(json.dumps(state)))
    assert everything(store.redis) == alone
    assert store.names.export("sw") == names
    assert store.redis.pttl(store.key("sw", "claim", "t1")) > 0


def test_a_snapshot_taken_before_names_were_exported_still_restores(store):
    from scripts.swarm import fencing

    seed(store, "sw")
    fencing.read(store, "sw")
    alone = everything(store.redis)
    state = store.export("sw")
    del state["names"]
    store.redis.flushall()
    store.restore("sw", json.loads(json.dumps(state)))
    assert everything(store.redis) == alone


def test_snapshot_remove_restore_gives_back_the_same_swarm_state(store):
    seed(store, "sw")
    seed(store, "other")
    before, codes = everything(store.redis), registered_codes(store)
    names = {slug: store.names.export(slug) for slug in ("sw", "other")}
    state = store.export("sw")
    for agent in store.agents("sw"):
        store.drop_agent("sw", agent.name)
    store.remove("sw")
    assert store.slugs() == ["other"]
    store.restore("sw", json.loads(json.dumps(state)))
    assert everything(store.redis) == before
    assert registered_codes(store) == codes
    assert {slug: store.names.export(slug) for slug in ("sw", "other")} == names


def test_a_restore_over_a_listed_swarm_lists_its_old_style_names_again(store: RedisStore) -> None:
    seed(store, "sw")
    store.seats.occupy("eng-2@sw", "sw-eng-7", 11)
    state = store.export("sw")
    store.redis.flushall()
    assert store.seats.agent_names("sw") == []
    store.restore("sw", json.loads(json.dumps(state)))
    assert ("sw-eng-7", "eng-2@sw") in store.seats.agent_seats("sw")


def test_export_leaves_out_another_swarms_seats_and_inbox(store):
    seed(store, "sw")
    seed(store, "sw-b")
    keys = set(store.export("sw")["keys"])
    assert keys and not any("sw-b" in key for key in keys)


def test_export_reads_no_key_of_another_swarm() -> None:
    import re

    import fakeredis

    from scripts.inbox.seats import SCAN_PAGE

    class Recording(fakeredis.FakeRedis):
        touched: list[tuple[str, ...]] = []
        scans: list[tuple[str, ...]] = []
        on = False

        def execute_command(self, *args: object, **options: object) -> object:
            if self.on:
                words = tuple(str(arg) for arg in args)
                (self.scans if words[0].upper() == "SCAN" else self.touched).append(words)
            return super().execute_command(*args, **options)

    store = RedisStore(Recording(decode_responses=True))
    seed(store, "sw")
    seed(store, "sw-b")
    other = store.names.code_of("sw-b")
    theirs = set(store.export("sw-b")["keys"])
    eng = next(agent.name for agent in store.agents("sw") if agent.lane == "eng")
    Recording.touched.clear()
    Recording.on = True
    keys = set(store.export("sw")["keys"])
    Recording.on = False
    assert InboxStore(store.redis).key("address", eng) in keys
    read = {arg for words in Recording.touched for arg in words[1:]}
    assert theirs and not read & theirs
    assert [arg for arg in read if "sw-b" in arg or re.search(rf"\b{other}\b", arg)] == []
    assert Recording.scans and all("COUNT" in words[-2] and words[-1] == str(SCAN_PAGE) for words in Recording.scans)
    assert all(any("MATCH" in word for word in words) for words in Recording.scans)


def test_export_keeps_the_mail_of_a_named_agent_without_a_seat(store: RedisStore) -> None:
    seed(store, "sw")
    name = store.next_name("sw", "eng")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", name, "start on the parser")
    assert inbox.key("item", item.id) in store.export("sw")["keys"]


def test_export_keeps_the_mail_of_a_seat_that_holds_only_memory(store: RedisStore) -> None:
    seed(store, "sw")
    store.memory.learn("eng-3@sw", "someone", "read the plan first", 30)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", "eng-3@sw", "pick this up")
    assert inbox.key("item", item.id) in store.export("sw")["keys"]


def test_export_leaves_out_the_tick_lock_and_keeps_its_other_keys(store: RedisStore) -> None:
    seed(store, "sw")
    store.redis.set(store.key("sw", "tick-lock"), "held")
    store.redis.set(store.key("sw", ""), "bare")
    store.culture.set("sw", "ship small")
    keys = store.export("sw")["keys"]
    assert store.key("sw", "tick-lock") not in keys
    assert store.key("sw", "") in keys
    assert store.culture.key("sw") in keys


@pytest.mark.feature("swarm-snapshot-atomic-read")
def test_a_delivery_closing_during_export_never_pairs_an_old_item_with_a_newer_journal() -> None:
    state, inbox, item_id, _ = close_mid_export(
        lambda inbox, item_id, eng: (
            lambda words: inbox.key("item", item_id) in words[1:] and words[0] not in ("TYPE", "EXISTS")
        )
    )
    item_key, history_key = inbox.key("item", item_id), inbox.key("history", item_id)
    keys, (before, after) = state["keys"], state["seen"]
    assert before[item_key] != after[item_key]
    pair = (keys[item_key]["value"], keys[history_key]["value"])
    assert pair in ((before[item_key], before[history_key]), (after[item_key], after[history_key]))


@pytest.mark.feature("swarm-snapshot-atomic-read")
def test_a_delivery_closing_during_export_never_leaves_its_address_waiting_without_pending_mail() -> None:
    state, inbox, _, eng = close_mid_export(
        lambda inbox, item_id, eng: lambda words: words[:3] == ["SISMEMBER", inbox.key("waiting"), eng]
    )
    waiting = eng in state["members"].get(inbox.key("waiting"), [])
    assert waiting == (inbox.key("pending", eng) in state["keys"])


@pytest.mark.feature("swarm-snapshot-atomic-read")
def test_export_carries_each_shared_inbox_membership_of_the_swarm_and_none_of_another(store: RedisStore) -> None:
    seed(store, "sw")
    eng = next(agent.name for agent in store.agents("sw") if agent.lane == "eng")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", eng, "index me")
    for shared in ("waiting", "indexed", "open-indexed"):
        store.redis.sadd(inbox.key(shared), eng, "someone@elsewhere")
    store.redis.sadd(inbox.key("wake-outbox"), item.id, "foreign-item")
    members = store.export("sw")["members"]
    for shared in ("waiting", "indexed", "open-indexed"):
        assert eng in members[inbox.key(shared)]
        assert "someone@elsewhere" not in members[inbox.key(shared)]
    assert item.id in members[inbox.key("wake-outbox")]
    assert "foreign-item" not in members[inbox.key("wake-outbox")]


def close_mid_export(
    trigger: Callable[[InboxStore, str, str], Callable[[list[str]], bool]],
) -> tuple[dict[str, Any], InboxStore, str, str]:
    """Export swarm sw while a delivery to its engineer closes right after the first command the trigger matches; the
    export also carries what the item key and its history held before and after the close under seen."""
    import fakeredis

    server = fakeredis.FakeServer()
    other = fakeredis.FakeRedis(server=server, decode_responses=True)

    armed: list[Callable[[list[str]], bool]] = []

    class Closing(fakeredis.FakeRedis):
        def execute_command(self, *args: object, **options: object) -> object:
            result = super().execute_command(*args, **options)
            if armed and armed[0]([str(arg) for arg in args]):
                armed.clear()
                inbox.close(item.id, eng, "done", "answered while the snapshot read")
            return result

    store = RedisStore(Closing(server=server, decode_responses=True))
    seed(store, "sw")
    eng = next(agent.name for agent in store.agents("sw") if agent.lane == "eng")
    inbox = InboxStore(other)
    item = inbox.send("operator", eng, "close me while the snapshot reads")
    read = [inbox.key("item", item.id), inbox.key("history", item.id)]
    before = dict(zip(read, (other.hgetall(read[0]), other.lrange(read[1], 0, -1))))
    armed.append(trigger(inbox, item.id, eng))
    state = store.export("sw")
    after = dict(zip(read, (other.hgetall(read[0]), other.lrange(read[1], 0, -1))))
    return {**state, "seen": (before, after)}, inbox, item.id, eng


def test_snapshot_writes_one_document_with_the_ledger_and_worktrees(store, tmp_path, monkeypatch):
    seed(store, "sw")
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    legacy_page.store(tmp_path, "sw", {"tasks": [{"id": "t1"}]})
    listing = "worktree /repo\nbranch refs/heads/dev\n\nworktree /wt/engineer-a1b2c3-0001\nbranch refs/heads/engineer-a1b2c3-0001\n"

    def git(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, listing, "")

    path = snapshot.take(store, "sw", 99, run=git)
    doc = json.loads(path.read_text())
    assert path == snapshot.path("sw") and path.parent.name == "sw"
    assert doc["ledger"] == {"tasks": [{"id": "t1"}], "_meta": {"rev": 1}}
    assert doc["worktrees"] == {"engineer@a1b2c3-0001": "/wt/engineer-a1b2c3-0001", "master@a1b2c3-0001": ""}
    assert _values(doc["state"]) == _values(json.loads(json.dumps(store.export("sw"))))


def test_a_host_without_git_finds_no_worktrees():
    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "git")

    assert snapshot.worktrees("github.com/o/r", ["engineer@a1b2c3-0001"], missing) == {"engineer@a1b2c3-0001": ""}


def test_worktrees_list_the_repo_in_porcelain_text_with_a_bound():
    calls = []

    def git(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "worktree /wt/a\nbranch refs/heads/engineer-a1b2c3-0001\n", "")

    assert snapshot.worktrees("/repo", ["engineer@a1b2c3-0001"], git) == {"engineer@a1b2c3-0001": "/wt/a"}
    assert calls == [
        (
            ["git", "-C", "/repo", "worktree", "list", "--porcelain"],
            {"capture_output": True, "text": True, "timeout": 30},
        )
    ]


def test_restore_refuses_while_an_agent_of_the_swarm_is_live(store):
    seed(store, "sw")
    snapshot.take(store, "sw", 99, run=_no_git)
    before = everything(store.redis)
    with pytest.raises(SwarmError, match="engineer@a1b2c3-0001"):
        snapshot.restore(store, "sw", live={"engineer@a1b2c3-0001"})
    assert everything(store.redis) == before


def test_pending_inbox_items_survive_a_lost_redis(store: RedisStore) -> None:
    item = seed(store, "sw")
    pending = [i.id for i in InboxStore(store.redis).pending_items("master@sw")]
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    inbox = InboxStore(store.redis)
    assert [i.id for i in inbox.pending_items("master@sw")] == pending
    assert item.id in {i.id for i in inbox.pending()}
    assert [h["state"] for h in inbox.history(item.id)] == ["pending"]


@pytest.mark.feature("swarm-snapshot-inbox-receipts")
def test_a_delivery_in_flight_when_redis_is_lost_is_delivered_once_after_restore(store: RedisStore) -> None:
    item = seed(store, "sw")
    dispatcher = Dispatcher(InboxStore(store.redis))
    dispatcher.own("master@sw", "bridge-1")
    deliveries = dispatcher.reserve("master@sw", "bridge-1")
    assert item.id in {delivery.item for delivery in deliveries}
    for delivery in deliveries:
        dispatcher.receipts.submitting(delivery.id, "bridge-1")
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    restored = Dispatcher(InboxStore(store.redis))
    restored.own("master@sw", "bridge-1")
    assert restored.reserve("master@sw", "bridge-1") == []
    recovered = restored.receipts.recover("master@sw", "bridge-1")
    assert sorted((d.id, d.state) for d in recovered) == sorted((d.id, "unknown") for d in deliveries)
    for delivery in deliveries:
        assert restored.receipts.accept(delivery.id, "bridge-1", delivery.digest).committed
    assert restored.reserve("master@sw", "bridge-1") == []
    states = [h["state"] for h in InboxStore(store.redis).history(item.id)]
    assert states.count("delivered") == 1


@pytest.mark.feature("swarm-snapshot-inbox-receipts")
def test_a_ref_shown_before_redis_is_lost_is_not_delivered_again_after_restore(store: RedisStore) -> None:
    seed(store, "sw")
    inbox = InboxStore(store.redis)
    inbox.send("operator", "master@sw", "a comment on the parser", ref="sw:1:c1")
    dispatcher = Dispatcher(inbox)
    dispatcher.own("master@sw", "bridge-1")
    for delivery in dispatcher.reserve("master@sw", "bridge-1"):
        dispatcher.receipts.submitting(delivery.id, "bridge-1")
        dispatcher.receipts.accept(delivery.id, "bridge-1", delivery.digest)
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    restored = InboxStore(store.redis)
    again = restored.send("operator", "master@sw", "the same comment relayed again", ref="sw:1:c1")
    redelivery = Dispatcher(restored)
    redelivery.own("master@sw", "bridge-1")
    assert redelivery.reserve("master@sw", "bridge-1") == []
    assert restored.get(again.id).state == "done"


@pytest.mark.feature("swarm-snapshot-inbox-receipts")
def test_a_ref_in_flight_when_redis_is_lost_still_holds_its_ref_after_restore(store: RedisStore) -> None:
    seed(store, "sw")
    inbox = InboxStore(store.redis)
    inbox.send("operator", "master@sw", "a comment on the parser", ref="sw:1:c1")
    dispatcher = Dispatcher(inbox)
    dispatcher.own("master@sw", "bridge-1")
    for delivery in dispatcher.reserve("master@sw", "bridge-1"):
        dispatcher.receipts.submitting(delivery.id, "bridge-1")
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    restored = InboxStore(store.redis)
    again = restored.send("operator", "master@sw", "the same comment relayed again", ref="sw:1:c1")
    redelivery = Dispatcher(restored)
    redelivery.own("master@sw", "bridge-1")
    assert redelivery.reserve("master@sw", "bridge-1") == []
    assert restored.get(again.id).state == "pending"


@pytest.mark.feature("swarm-snapshot-inbox-receipts")
def test_an_alias_recipient_journal_is_exported_under_its_canonical_name(store: RedisStore) -> None:
    seed(store, "sw")
    eng = next(agent.name for agent in store.agents("sw") if agent.lane == "eng")
    store.names.alias("sw-eng-old", eng)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", "sw-eng-old", "carry on from the parser", ref="sw:2:a1")
    dispatcher = Dispatcher(inbox)
    dispatcher.own("sw-eng-old", "bridge-1")
    [delivery] = dispatcher.reserve("sw-eng-old", "bridge-1")
    dispatcher.receipts.submitting(delivery.id, "bridge-1")
    journal = {
        inbox.key("deliveries", eng),
        inbox.key("delivery", delivery.id),
        inbox.key("reservation", item.id),
        inbox.key("ref-reservation", eng, "sw:2:a1"),
    }
    alias_keys, _ = inbox.keys_at(["sw-eng-old"])
    assert not journal & set(alias_keys)
    assert journal <= set(inbox.keys_at([eng])[0])
    with patch.object(InboxStore, "keys_at", autospec=True, side_effect=InboxStore.keys_at) as listed:
        exported = store.export("sw")
    assert {"sw-eng-old", eng} <= set(listed.call_args.args[1])
    assert journal <= set(exported["keys"])
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    restored = Dispatcher(InboxStore(store.redis))
    restored.own("sw-eng-old", "bridge-1")
    assert restored.reserve("sw-eng-old", "bridge-1") == []
    assert [(d.id, d.state) for d in restored.receipts.recover("sw-eng-old", "bridge-1")] == [(delivery.id, "unknown")]


def admitted_task(store: RedisStore, ledger: FakeLedger, task_id: str, at: int) -> dict[str, Any]:
    """The launch payload hub admission hands the hive for ``task_id`` of swarm ``sw`` at ``at``."""
    row = ledger.rows[task_id]
    request = {"hive": readiness.ROOT, "task": task_id, "lane": row["lane"], "generation": readiness.generation(row)}
    source = SimpleNamespace(snapshot=lambda name: seat_view(store, ledger, name))
    admitted = admission.Admission(store, ledger, source, lambda: at).admit("sw", request)  # pyright: ignore[reportArgumentType]
    assert admitted and "task" in admitted, admitted
    return admitted["task"]


def test_a_restored_swarm_starts_paused_and_only_the_master_comes_up(store: RedisStore) -> None:
    seed(store, "sw")
    store.update("sw", state="running")
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    outcomes = snapshot.restore(store, "sw", live=set())
    assert [(o.name, o.outcome, o.reason) for o in outcomes] == [
        ("engineer@a1b2c3-0001", "awaiting-decision", "no conversation id"),
        ("master@a1b2c3-0001", "awaiting-decision", "no conversation id"),
    ]
    assert store.config("sw").state == "paused"
    assert {a.state for a in store.agents("sw")} == {"awaiting-decision"}
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}])
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0001")
    rt = FakeRuntime()
    assert resume.decide(store, "sw", "engineer@a1b2c3-0001", "fresh", rt, 9000, ledger).outcome == "fresh"
    assert resume.decide(store, "sw", "master@a1b2c3-0001", "fresh", rt, 9001, ledger).outcome == "fresh"
    actions = tick("sw", store, ledger, rt, 10_000)
    assert rt.spawned == [] and [m[0] for m in rt.masters] == ["master@a1b2c3-0002"]
    assert ledger.rows["t1"]["state"] == "open" and "retired engineer@a1b2c3-0001" in actions
    assert store.seats.occupant("master@sw").occupant == "master@a1b2c3-0002"
    store.update("sw", state="running")
    launched = [admitted_task(store, ledger, task_id, 20_000) for task_id in ("t1", "t2")]
    successor = next(t for t in launched if t["id"] == "t2")
    assert successor["handoff"] == "carry on from the parser" and successor["seat"] == "eng-2@sw"
    resumed = next(t for t in launched if t["id"] == "t1")
    assert resumed["learned"][0]["text"] == "run the gates twice"


def test_restore_without_a_snapshot_names_the_missing_document(store):
    with pytest.raises(SwarmError, match="snapshot"):
        snapshot.restore(store, "sw", live=set())


def test_restore_writes_the_snapshot_ledger_back_only_where_none_is_stored(store, tmp_path, monkeypatch):
    seed(store, "sw")
    for name in ("taken", "empty", "kept"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "taken")
    legacy_page.store(tmp_path / "taken", "sw", {"tasks": [{"id": "t1"}]})
    snapshot.take(store, "sw", 99, run=_no_git)
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "empty")
    snapshot.restore(store, "sw", live=set())
    assert snapshot.stored_ledger("sw") == {"tasks": [{"id": "t1"}], "_meta": {"rev": 1}}
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "kept")
    legacy_page.store(tmp_path / "kept", "sw", {"tasks": [{"id": "t9"}]})
    snapshot.restore(store, "sw", live=set())
    assert snapshot.stored_ledger("sw") == {"tasks": [{"id": "t9"}], "_meta": {"rev": 1}}


def test_each_agent_conversation_id_survives_a_snapshot_and_a_lost_redis(store):
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1", conversation_id="5c90d80c"))
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0002", "eng", "t2", pane_id="w1:p2"))
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    assert {a.name: a.conversation_id for a in store.agents("sw")} == {
        "engineer@a1b2c3-0001": "5c90d80c",
        "engineer@a1b2c3-0002": "",
    }


def test_an_agent_record_saved_before_conversation_ids_loads_with_an_empty_id(store):
    store.redis.hset(
        store.key("sw", "agents"),
        "engineer@a1b2c3-0001",
        json.dumps({"name": "engineer@a1b2c3-0001", "lane": "eng", "task": "t1"}),
    )
    assert store.agents("sw")[0].conversation_id == ""


def _values(state):
    return {key: (entry["type"], entry["value"]) for key, entry in state["keys"].items()}, state["members"]


def _no_git(argv, **kw):
    return subprocess.CompletedProcess(argv, 128, "", "not a git repository")


@pytest.fixture
def env(monkeypatch, store):
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}])
    ledger.say = lambda slug, text, by=None: None
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.join", lambda _, *args: ledger.join(*args))
    rt = FakeRuntime()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: True)
    monkeypatch.setattr(cli.snapshot, "worktrees", lambda repo, names, run=None: {})
    return store, ledger, rt


def test_stop_takes_a_snapshot_before_it_retires_anyone(env: tuple[RedisStore, FakeLedger, FakeRuntime]) -> None:
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    rt.live.add(seat_agent(store, ledger, "sw", "t1", hive=commands.hive_id()).name)
    assert cli.main(["sw", "stop", "--now"]) == 0
    doc = json.loads(snapshot.path("sw").read_text())
    agents = doc["state"]["keys"][store.key("sw", "agents")]["value"]
    assert sorted(agents) == ["engineer@a1b2c3-0001", "master@a1b2c3-0001"]


def test_snapshot_and_restore_commands_bring_the_swarm_back_paused(env, capsys):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    assert cli.main(["sw", "snapshot"]) == 0
    assert cli.main(["sw", "restore"]) == 1
    assert "live" in capsys.readouterr().err
    rt.live.clear()
    store.redis.flushall()
    assert cli.main(["sw", "restore"]) == 0
    assert store.config("sw").state == "paused"
    assert [m[0] for m in rt.masters] == ["master@a1b2c3-0001"]
    assert cli.main(["sw", "--as", "operator", "restore-decision", "master@a1b2c3-0001", "fresh"]) == 0
    assert cli.main(["tick"]) == 0
    assert [m[0] for m in rt.masters] == ["master@a1b2c3-0001", "master@a1b2c3-0002"]


MINUTE = 60_000


def _running(store, slug="sw", **changes):
    store.create(SwarmConfig(slug, "/repo", 2, 1))
    return store.update(slug, state="running", **changes)


def test_a_running_swarm_takes_its_first_automatic_snapshot_at_once(store):
    _running(store)
    taken = snapshot.auto(store, "sw", 1_000, {}, run=_no_git)
    assert taken == snapshot.auto_dir("sw") / "auto-1000.json"
    assert json.loads(taken.read_text())["taken_at"] == 1_000
    assert not snapshot.path("sw").exists()


def test_the_next_automatic_snapshot_waits_thirty_minutes_by_default(store):
    _running(store)
    snapshot.auto(store, "sw", 0, {}, run=_no_git)
    assert snapshot.auto(store, "sw", 30 * MINUTE - 1, {}, run=_no_git) is None
    assert snapshot.auto(store, "sw", 30 * MINUTE, {}, run=_no_git) is not None
    assert snapshot.last_auto("sw") == 30 * MINUTE


def test_the_environment_sets_the_interval_and_swarm_set_overrides_it(store):
    _running(store)
    env = {"AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES": "10"}
    snapshot.auto(store, "sw", 0, env, run=_no_git)
    assert snapshot.auto(store, "sw", 10 * MINUTE, env, run=_no_git) is not None
    store.update("sw", snapshot_minutes=5)
    assert snapshot.auto(store, "sw", 15 * MINUTE, env, run=_no_git) is not None
    assert snapshot.interval_minutes(store.config("sw"), env) == 5


def test_no_automatic_snapshot_while_paused_or_with_the_interval_at_zero(store):
    store.create(SwarmConfig("sw", "/repo", 2, 1, state="paused"))
    assert snapshot.auto(store, "sw", 0, {}, run=_no_git) is None
    store.update("sw", state="running", snapshot_minutes=0)
    assert snapshot.auto(store, "sw", 0, {}, run=_no_git) is None
    assert snapshot.automatic("sw") == []


def test_only_the_ten_newest_automatic_snapshots_are_kept(store):
    _running(store, snapshot_minutes=1)
    snapshot.take(store, "sw", 5, run=_no_git)
    for n in range(12):
        snapshot.auto(store, "sw", n * MINUTE, {}, run=_no_git)
    assert [p.name for p in snapshot.automatic("sw")] == [f"auto-{n * MINUTE}.json" for n in range(2, 12)]
    assert snapshot.path("sw").exists()


@pytest.mark.feature("swarm-auto-snapshot-size-budget")
def test_kept_automatic_snapshots_fit_the_size_budget_counted_from_the_newest(store, monkeypatch):
    monkeypatch.setattr(snapshot, "KEEP_BYTES", 1_000_000, raising=False)
    _running(store, snapshot_minutes=1)
    snapshot.auto_dir("sw").mkdir(parents=True)
    for n in range(1, 6):
        (snapshot.auto_dir("sw") / f"auto-{n * MINUTE}.json").write_text(json.dumps({"taken_at": n}).ljust(400_000))
    snapshot.auto(store, "sw", 10 * MINUTE, {}, run=_no_git)
    kept = snapshot.automatic("sw")
    assert [p.name for p in kept] == [f"auto-{n * MINUTE}.json" for n in (4, 5, 10)]
    assert sum(p.stat().st_size for p in kept) <= 1_000_000


@pytest.mark.feature("swarm-auto-snapshot-size-budget")
def test_the_newest_automatic_snapshot_is_kept_even_past_the_size_budget(tmp_path):
    files = [tmp_path / f"auto-{n}.json" for n in (1, 2)]
    for file in files:
        with file.open("wb") as planted:
            planted.truncate(3 << 30)
    assert snapshot.stale(files) == files[:1]


@pytest.mark.parametrize("slug", ["", ".", "..", "sw/..", "/abs"])
def test_automatic_pruning_refuses_a_name_that_is_not_one_swarm_folder(store, slug):
    _running(store, slug=slug, snapshot_minutes=1)
    shared = snapshot.Path.home() / ".agentihooks" / "swarm" / "snapshots"
    shared.mkdir(parents=True)
    planted = [shared / f"auto-{n}.json" for n in range(1, 12)]
    for file in planted:
        file.write_text(json.dumps({"taken_at": 0}))
    with pytest.raises(SwarmError, match="refusing snapshot path for swarm .*: not one folder under"):
        snapshot.auto(store, slug, 20 * MINUTE, {}, run=_no_git)
    assert all(file.exists() for file in planted)


def test_a_swarm_snapshot_lives_in_its_own_folder_under_the_swarm_root():
    assert snapshot.path("sw") == snapshot.Path.home() / ".agentihooks" / "swarm" / "sw" / "snapshot.json"


def test_restore_uses_the_newest_snapshot_unless_pointed_at_another(store):
    _running(store, snapshot_minutes=1)
    store.update("sw", max_eng=3)
    snapshot.auto(store, "sw", MINUTE, {}, run=_no_git)
    store.update("sw", max_eng=4)
    snapshot.take(store, "sw", 2 * MINUTE, run=_no_git)
    store.update("sw", max_eng=5)
    newest = snapshot.auto(store, "sw", 3 * MINUTE, {}, run=_no_git)
    assert snapshot.newest("sw") == newest
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    assert store.config("sw").max_eng == 5
    snapshot.restore(store, "sw", live=set(), source=snapshot.path("sw"))
    assert store.config("sw").max_eng == 4
    snapshot.restore(store, "sw", live=set(), source=snapshot.automatic("sw")[0])
    assert store.config("sw").max_eng == 3


def test_the_tick_takes_an_automatic_snapshot_and_restore_from_names_an_older_one(env, capsys):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    assert [p.name.startswith("auto-") for p in snapshot.automatic("sw")] == [True]
    assert "automatic snapshot" in capsys.readouterr().out
    older = snapshot.automatic("sw")[0]
    cli.main(["sw", "snapshot"])
    rt.live.clear()
    assert cli.main(["sw", "restore", "--from", str(older)]) == 0
    assert str(older) in capsys.readouterr().out


def test_swarm_set_takes_the_snapshot_interval(env, capsys):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    assert cli.main(["sw", "set", "snapshot-minutes=15"]) == 0
    assert store.config("sw").snapshot_minutes == 15


def test_status_shows_when_the_last_automatic_snapshot_was_taken(env, capsys):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "status"])
    assert "no automatic snapshot yet  every 30 min" in capsys.readouterr().out
    snapshot.take(
        store, "sw", 1_791_206_100_000, run=_no_git, target=snapshot.auto_dir("sw") / "auto-1791206100000.json"
    )
    cli.main(["sw", "status"])
    out = capsys.readouterr().out
    assert "last automatic snapshot 2026-10-05 13:15 UTC" in out and "kept 1" in out
    cli.main(["sw", "status", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["auto_snapshot"] == {"last": 1_791_206_100_000, "kept": 1, "every_minutes": 30}


def test_the_ledger_source_names_the_show_command_for_its_slug():
    assert snapshot.ledger_source("sw") == "agentihooks ledger --slug sw show"


@pytest.mark.feature("snapshot-job-bounded-memory")
def test_a_snapshot_is_written_one_entry_at_a_time_never_as_one_whole_string(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running(store)
    for n in range(20):
        store.redis.set(store.key("sw", f"bulk-{n}"), "x" * 50_000)
    real, longest = json.dumps, [0]

    def measured(value: object, *args: object, **kwargs: object) -> str:
        text = real(value, *args, **kwargs)
        longest[0] = max(longest[0], len(text))
        return text

    monkeypatch.setattr(json, "dumps", measured)
    taken = snapshot.take(store, "sw", 99, run=_no_git, environ={})
    monkeypatch.setattr(json, "dumps", real)
    written = taken.read_text()
    assert json.loads(written)["state"]["keys"][store.key("sw", "bulk-3")]["value"] == "x" * 50_000
    assert longest[0] < len(written) / 10


@pytest.mark.feature("snapshot-job-bounded-memory")
def test_pruning_reads_each_kept_snapshot_refs_without_parsing_the_snapshot(store: RedisStore) -> None:
    _running(store, snapshot_minutes=1)
    taken = snapshot.auto(store, "sw", 0, {}, run=_no_git)
    assert taken is not None
    assert json.loads(taken.with_name(taken.name + ".refs").read_text()) == []
    taken.write_text("{")
    assert snapshot.referenced("sw") == set()


@pytest.mark.feature("snapshot-job-bounded-memory")
def test_an_automatic_snapshot_dropped_from_the_ten_takes_its_refs_with_it(store: RedisStore) -> None:
    _running(store, snapshot_minutes=1)
    for n in range(12):
        snapshot.auto(store, "sw", n * MINUTE, {}, run=_no_git)
    kept = sorted(p.name for p in snapshot.auto_dir("sw").glob("*.refs"))
    assert kept == sorted(f"auto-{n * MINUTE}.json.refs" for n in range(2, 12))


@pytest.mark.feature("snapshot-job-bounded-memory")
def test_a_take_that_dies_before_its_snapshot_lands_leaves_no_refs_naming_the_old_snapshot(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running(store)
    target = snapshot.take(store, "sw", 1, run=_no_git, environ={})
    held = {"sources": {"a": {"inventory": {"x": {"sha256": "kept-by-old"}}}}}
    target.write_text(json.dumps(held))
    real = Path.replace

    def dies(self: Path, other: Path) -> Path:
        if self.suffix == ".partial":
            raise OSError("killed")
        return real(self, other)

    monkeypatch.setattr(Path, "replace", dies)
    with pytest.raises(OSError):
        snapshot.take(store, "sw", 2, run=_no_git, environ={})
    assert snapshot.referenced("sw") == {"kept-by-old"}


@pytest.mark.feature("snapshot-job-bounded-memory")
def test_each_entry_down_to_the_split_depth_is_written_as_its_own_string() -> None:
    chunks: list[str] = []

    class Out:
        def write(self, text: str) -> int:
            chunks.append(text)
            return len(text)

    value = {"keys": {"a": {"type": "hash", "value": {"f": "1"}}, "b": [1]}, "names": None}
    snapshot._write(cast("TextIO", Out()), value, 2)
    assert chunks == [
        "{",
        '"keys": ',
        "{",
        '"a": ',
        '{"type": "hash", "value": {"f": "1"}}',
        ', "b": ',
        "[1]",
        "}",
        ', "names": ',
        "null",
        "}",
    ]
    assert json.loads("".join(chunks)) == value
