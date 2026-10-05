import json
import subprocess

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, snapshot
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


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


def test_export_then_restore_after_losing_redis_gives_back_the_same_swarm_state(store):
    seed(store, "sw")
    alone = everything(store.redis)
    seed(store, "other")
    state = store.export("sw")
    store.redis.flushall()
    store.restore("sw", json.loads(json.dumps(state)))
    assert everything(store.redis) == alone
    assert store.redis.pttl(store.key("sw", "claim", "t1")) > 0


def test_snapshot_remove_restore_gives_back_the_same_swarm_state(store):
    seed(store, "sw")
    seed(store, "other")
    before = everything(store.redis)
    state = store.export("sw")
    for agent in store.agents("sw"):
        store.drop_agent("sw", agent.name)
    store.remove("sw")
    assert store.slugs() == ["other"]
    store.restore("sw", json.loads(json.dumps(state)))
    assert everything(store.redis) == before


def test_export_leaves_out_another_swarms_seats_and_inbox(store):
    seed(store, "sw")
    seed(store, "sw-b")
    keys = set(store.export("sw")["keys"])
    assert keys and not any("sw-b" in key for key in keys)


def test_snapshot_writes_one_document_with_the_ledger_and_worktrees(store, tmp_path, monkeypatch):
    seed(store, "sw")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    (tmp_path / "sw.json").write_text(json.dumps({"tasks": [{"id": "t1"}]}))
    listing = "worktree /repo\nbranch refs/heads/dev\n\nworktree /wt/sw-eng-1\nbranch refs/heads/sw-eng-1\n"

    def git(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, listing, "")

    path = snapshot.take(store, "sw", 99, run=git)
    doc = json.loads(path.read_text())
    assert path == snapshot.path("sw") and path.parent.name == "sw"
    assert doc["ledger"] == {"tasks": [{"id": "t1"}]}
    assert doc["worktrees"] == {"sw-eng-1": "/wt/sw-eng-1", "sw-master-1": ""}
    assert _values(doc["state"]) == _values(json.loads(json.dumps(store.export("sw"))))


def test_restore_refuses_while_an_agent_of_the_swarm_is_live(store):
    seed(store, "sw")
    snapshot.take(store, "sw", 99, run=_no_git)
    before = everything(store.redis)
    with pytest.raises(SwarmError, match="sw-eng-1"):
        snapshot.restore(store, "sw", live={"sw-eng-1"})
    assert everything(store.redis) == before


def test_pending_inbox_items_survive_a_lost_redis(store):
    item = seed(store, "sw")
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    snapshot.restore(store, "sw", live=set())
    inbox = InboxStore(store.redis)
    assert [i.id for i in inbox.pending_items("master@sw")] == [item.id]
    assert item.id in {i.id for i in inbox.pending()}
    assert [h["state"] for h in inbox.history(item.id)] == ["pending"]


def test_a_restored_swarm_starts_paused_and_only_the_master_comes_up(store):
    seed(store, "sw")
    store.update("sw", state="running")
    snapshot.take(store, "sw", 99, run=_no_git)
    store.redis.flushall()
    finished = snapshot.restore(store, "sw", live=set())
    assert finished == ["sw-eng-1", "sw-master-1"]
    assert store.config("sw").state == "paused"
    assert {a.state for a in store.agents("sw")} == {"finished"}
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}])
    ledger.rows["t1"].update(state="claimed", claimed_by="sw-eng-1")
    rt = FakeRuntime()
    actions = tick("sw", store, ledger, rt, 10_000)
    assert rt.spawned == [] and [m[0] for m in rt.masters] == ["sw-master-2"]
    assert ledger.rows["t1"]["state"] == "open" and "retired sw-eng-1" in actions
    assert store.seats.occupant("master@sw").occupant == "sw-master-2"
    store.update("sw", state="running")
    tick("sw", store, ledger, rt, 20_000)
    successor = next(t for t in rt.tasks if t["id"] == "t2")
    assert successor["handoff"] == "carry on from the parser" and successor["seat"] == "eng-2@sw"
    resumed = next(t for t in rt.tasks if t["id"] == "t1")
    assert resumed["learned"][0]["text"] == "run the gates twice"


def test_restore_without_a_snapshot_names_the_missing_document(store):
    with pytest.raises(SwarmError, match="snapshot"):
        snapshot.restore(store, "sw", live=set())


def _values(state):
    return {key: (entry["type"], entry["value"]) for key, entry in state["keys"].items()}, state["members"]


def _no_git(argv, **kw):
    return subprocess.CompletedProcess(argv, 128, "", "not a git repository")


@pytest.fixture
def env(monkeypatch, store):
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}])
    ledger.say = lambda slug, text, by=None: None
    rt = FakeRuntime()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: True)
    monkeypatch.setattr(cli.snapshot, "worktrees", lambda repo, names, run=None: {})
    return store, ledger, rt


def test_stop_takes_a_snapshot_before_it_retires_anyone(env):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    assert cli.main(["sw", "stop", "--now"]) == 0
    doc = json.loads(snapshot.path("sw").read_text())
    agents = doc["state"]["keys"][store.key("sw", "agents")]["value"]
    assert sorted(agents) == ["sw-eng-1", "sw-master-1"]


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
    assert [m[0] for m in rt.masters] == ["sw-master-1", "sw-master-2"]
