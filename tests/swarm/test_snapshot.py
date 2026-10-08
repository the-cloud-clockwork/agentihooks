import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, resume, snapshot
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm_ledger import legacy_page

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core as core  # noqa: E402

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


def test_restore_refuses_while_an_agent_of_the_swarm_is_live(store):
    seed(store, "sw")
    snapshot.take(store, "sw", 99, run=_no_git)
    before = everything(store.redis)
    with pytest.raises(SwarmError, match="engineer@a1b2c3-0001"):
        snapshot.restore(store, "sw", live={"engineer@a1b2c3-0001"})
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
    tick("sw", store, ledger, rt, 20_000)
    successor = next(t for t in rt.tasks if t["id"] == "t2")
    assert successor["handoff"] == "carry on from the parser" and successor["seat"] == "eng-2@sw"
    resumed = next(t for t in rt.tasks if t["id"] == "t1")
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
