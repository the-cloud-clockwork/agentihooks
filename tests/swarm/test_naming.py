import ast
import re
from pathlib import Path

import pytest

from scripts.herdr_host import agent_name
from scripts.inbox import exits
from scripts.inbox.seats import is_seat, master_of, of_swarm, seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import naming
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

ROOT = Path(__file__).resolve().parents[2]
HERDR_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def store(redis):
    s = RedisStore(redis)
    s.create(SwarmConfig("sw", "/home/x/dev/agentihooks", max_eng=2, max_ci=1))
    return s


def test_build_and_parse_round_trip():
    name = naming.build("engineer", "a1b2c3", 2)
    assert name == "engineer@a1b2c3-0002"
    parsed = naming.parse(name)
    assert (parsed.kind, parsed.code, parsed.number, parsed.lane) == ("engineer", "a1b2c3", 2, "eng")
    assert naming.parse("sw-eng-1") is None
    assert naming.parse("eng-1@sw") is None
    assert naming.lane_of("master@a1b2c3-0001") == MASTER
    assert naming.lane_of("ci@a1b2c3-0001") == "ci"
    assert naming.lane_of("sw-eng-3") == "eng"
    assert naming.lane_of("operator") == ""


def test_creating_a_swarm_mints_one_code_kept_on_its_record_and_in_the_registry(store):
    code = store.config("sw").code
    assert re.fullmatch(r"[0-9a-f]{6}", code)
    assert store.names.swarm(code) == {
        "swarm": "sw",
        "name": f"swarm@{code}",
        "ledger": "sw",
        "repo": "/home/x/dev/agentihooks",
    }
    assert store.names.code_of("sw") == code


def test_a_clashing_code_is_minted_again(redis):
    minted = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    names = naming.NameRegistry(redis, mint=lambda: next(minted))
    assert names.mint_code("one", "one", "/r") == "aaaaaa"
    assert names.mint_code("two", "two", "/r") == "bbbbbb"
    assert names.mint_code("one", "one", "/r") == "aaaaaa"


def test_an_existing_swarm_without_a_code_gets_one(store):
    store.redis.hset(store.key("sw", "config"), "code", "")
    store.redis.hdel(naming.NameRegistry.key("code-of"), "sw")
    assert store.ensure_code("sw").code
    assert store.config("sw").code == store.ensure_code("sw").code


def test_each_type_counts_on_its_own_in_four_digits_never_reused(store):
    code = store.config("sw").code
    got = [store.next_name("sw", lane) for lane in ("eng", "eng", "ci", MASTER, MASTER, "eng")]
    assert got == [
        f"engineer@{code}-0001",
        f"engineer@{code}-0002",
        f"ci@{code}-0001",
        f"master@{code}-0001",
        f"master@{code}-0002",
        f"engineer@{code}-0003",
    ]


def test_the_registry_keeps_type_number_session_spawn_and_retire(store):
    name = store.next_name("sw", "eng", at=5)
    store.names.note(name, session_id="abc")
    store.put_agent("sw", AgentRecord(name, "eng", "t1"))
    store.drop_agent("sw", name, at=9)
    row = store.names.entry(name)
    assert (row["type"], row["number"], row["session_id"], row["spawned_at"], row["retired_at"]) == (
        "engineer",
        1,
        "abc",
        5,
        9,
    )
    assert [r["name"] for r in store.names.names("sw")] == [name]


def test_a_name_is_an_inbox_address_not_a_seat(store):
    name = store.next_name("sw", "eng")
    assert not is_seat(name)
    assert is_seat(seat_address("sw", "eng-1"))
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", name, "hello")
    assert [i.id for i in inbox.pending_items(name)] == [item.id]
    assert inbox.acts_for(name, name)
    assert of_swarm(name, "sw", store.names)
    assert master_of(name, store.names) == seat_address("sw", MASTER)


def test_herdr_takes_the_name_in_its_own_form():
    assert agent_name("engineer@a1b2c3-0002") == "engineer-a1b2c3-0002"
    assert HERDR_NAME.fullmatch(agent_name("engineer@a1b2c3-0002"))


def test_the_swarm_space_is_repo_dash_code():
    assert naming.space("/home/x/dev/agentihooks", "a1b2c3") == "agentihooks-a1b2c3"
    assert naming.space("/home/x/dev/agentihooks/", "a1b2c3") == "agentihooks-a1b2c3"


def test_a_pending_item_passes_to_the_successor_of_the_same_type(store):
    old = store.next_name("sw", MASTER, at=1)
    store.put_agent("sw", AgentRecord(old, MASTER, MASTER, seat=seat_address("sw", MASTER)))
    store.seats.occupy(seat_address("sw", MASTER), old, 1)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", old, "still there?")
    store.drop_agent("sw", old, at=2)
    new = store.next_name("sw", MASTER, at=3)
    store.put_agent("sw", AgentRecord(new, MASTER, MASTER, seat=seat_address("sw", MASTER)))
    store.next_name("sw", "eng", at=3)
    exits.settle(inbox, old, "", "exited")
    assert inbox.get(item.id).address == new
    assert store.names.successor(old) == new


OLD_NAME = re.compile(
    r"-\(?(?:\?:)?(?:eng|ci|master)(?:\|[a-z]+)*\)?-(?:\\d|\d|\{|\*)"
    r"|\}-\{(?:lane|MASTER)\}-"
    r"|(?:master|engineer|ci)@\{[^}]*\}-\{"
    r"|@\[0-9a-f\]"
)
NAMING = ROOT / "scripts" / "swarm" / "naming.py"


def _strings(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, ast.unparse(node)


def name_sites(paths):
    found = []
    for path in paths:
        if path.resolve() == NAMING:
            continue
        for line, text in _strings(ast.parse(path.read_text(encoding="utf-8"))):
            if OLD_NAME.search(text):
                found.append(f"{path}:{line}: {text[:80]}")
    return found


def test_no_agent_name_is_built_or_parsed_outside_the_naming_module():
    paths = [p for tree in ("scripts", "hooks") for p in sorted((ROOT / tree).rglob("*.py"))]
    assert len(paths) > 100
    assert name_sites(paths) == []


@pytest.mark.parametrize(
    "planted",
    [
        'name = f"{slug}-{lane}-{n}"\n',
        'MASTER_RE = re.compile(r"-master-\\d+$")\n',
        'WORKER_RE = re.compile(r"-(eng|ci)-\\d+$")\n',
        'ok = by.startswith(f"{slug}-{MASTER}-")\n',
        'name = f"engineer@{code}-{n:04d}"\n',
    ],
)
def test_the_gate_turns_red_on_a_planted_name(tmp_path, planted):
    planted_file = tmp_path / "planted.py"
    planted_file.write_text(planted, encoding="utf-8")
    assert name_sites([planted_file])


def test_a_swarm_without_a_code_gets_one_at_its_next_tick_and_spawns_new_names(store):
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store.redis.hset(store.key("sw", "config"), "code", "")
    store.redis.hdel(naming.NameRegistry.key("code-of"), "sw")
    rt = FakeRuntime()
    tick("sw", store, FakeLedger([{"id": "t1"}]), rt, 1_000)
    code = store.config("sw").code
    assert code and code != "a1b2c3"
    assert [name for _, name, _ in rt.spawned] == [f"engineer@{code}-0001"]
    assert [name for name, _ in rt.masters] == [f"master@{code}-0001"]
    assert store.names.entry(f"engineer@{code}-0001")["spawned_at"] == 1_000


def test_health_and_ledger_events_read_the_type_from_the_name(store):
    from scripts.swarm import ledger_events
    from scripts.swarm.health import findings

    master = store.next_name("sw", MASTER)
    engineer = store.next_name("sw", "eng")
    assert findings._is_master(master) and not findings._is_worker(master)
    assert findings._is_worker(engineer) and findings._is_worker("sw-ci-2")
    mail = ledger_events.Mail(InboxStore(store.redis), store, "sw")
    assert not ledger_events._by_agent(mail, {"by": master})
    assert ledger_events._by_agent(mail, {"by": engineer})
    assert not ledger_events._by_agent(mail, {"by": "operator"})


def test_the_worktree_step_names_a_branch_git_and_wt_accept():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0002", {"id": "t1", "title": "x"})
    assert "wt.sh new engineer-a1b2c3-0002" in text
    assert "You are engineer@a1b2c3-0002" in text


def test_names_lists_the_code_space_and_every_name(store, capsys):
    from argparse import Namespace

    from scripts.swarm import cli

    name = store.next_name("sw", "eng", at=7)
    cli.cmd_names(store, Namespace(slug="sw", json=False))
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "name swarm@a1b2c3\tcode a1b2c3\tspace agentihooks-a1b2c3"
    assert out.splitlines()[1].startswith(f"{name}\tengineer\t1\t-\t7\t-")


PROOF = "proof-a1b2c3-dn1-1"


@pytest.fixture
def proof_store(redis):
    s = RedisStore(redis)
    s.create(SwarmConfig(PROOF, "/home/x/dev/agentihooks", max_eng=1, max_ci=0))
    return s


def test_names_json_and_a_proof_swarm_space(store, proof_store, capsys):
    import json
    from argparse import Namespace

    from scripts.swarm import cli

    cli.cmd_names(store, Namespace(slug="sw", json=True))
    listed = json.loads(capsys.readouterr().out)
    assert (listed["code"], listed["space"], listed["names"]) == ("a1b2c3", "agentihooks-a1b2c3", [])
    cli.cmd_names(proof_store, Namespace(slug=PROOF, json=False))
    assert capsys.readouterr().out.splitlines()[0].endswith(f"\tspace {PROOF}")
    cli.cmd_names(proof_store, Namespace(slug=PROOF, json=True))
    assert json.loads(capsys.readouterr().out)["space"] == PROOF


def test_a_proof_swarm_space_is_closed_and_renamed_by_its_slug(proof_store):
    from types import SimpleNamespace

    from scripts.swarm.rename import rename_swarm
    from scripts.swarm.runtime import HerdrRuntime

    calls, spaces = [], [{"workspace_id": "w5", "label": f"swarm-{PROOF}"}]

    def herdr(argv):
        calls.append(argv)
        if argv == ["workspace", "list"]:
            return {"workspaces": spaces}
        return {"agents": []} if argv == ["agent", "list"] else {}

    runtime = HerdrRuntime(herdr=herdr)
    rename_swarm(proof_store, PROOF, SimpleNamespace(tasks=lambda slug: []), runtime, 10)
    assert ["workspace", "rename", "w5", PROOF] in calls
    spaces[0]["label"] = PROOF
    assert runtime.close_space(proof_store.config(PROOF)) is True
    assert ["workspace", "close", "w5"] in calls
    spaces[0]["label"] = "agentihooks-ffffff"
    assert runtime.close_space(proof_store.config(PROOF)) is False


def test_resolve_many_reads_every_alias_in_one_round_trip(store, redis, monkeypatch):
    new = store.next_name("sw", "eng")
    store.names.alias("sw-eng-1", new)
    reads = []
    mget = redis.mget
    monkeypatch.setattr(redis, "get", lambda key: reads.append(key))
    monkeypatch.setattr(redis, "mget", lambda keys: reads.append(list(keys)) or mget(keys))
    keys = [f"{naming.PREFIX}:alias:{name}" for name in ("sw-eng-1", new, "stranger")]
    assert naming.NameRegistry(redis).resolve_many(["sw-eng-1", new, "stranger"]) == {
        "sw-eng-1": new,
        new: new,
        "stranger": "stranger",
    }
    assert reads == [keys]
    assert naming.NameRegistry(redis).resolve_many([]) == {}
    assert reads == [keys]


def test_resolve_many_keeps_every_name_when_redis_fails(redis, monkeypatch, caplog):
    from redis.exceptions import RedisError

    def down(keys):
        raise RedisError("down")

    monkeypatch.setattr(redis, "mget", down)
    assert naming.NameRegistry(redis).resolve_many(["a", "b"]) == {"a": "a", "b": "b"}
    assert "alias lookup failed for 2 names: down" in caplog.text


def test_resolve_names_needs_no_redis(monkeypatch):
    import hooks._redis

    monkeypatch.setattr(hooks._redis, "get_redis", lambda: None)
    assert naming.resolve_names(["a", "b"]) == {"a": "a", "b": "b"}


def test_resolve_names_reads_aliases_through_the_registry(store, redis, monkeypatch):
    import hooks._redis

    new = store.next_name("sw", "eng")
    store.names.alias("sw-eng-1", new)
    monkeypatch.setattr(hooks._redis, "get_redis", lambda: redis)
    assert naming.resolve_names(["sw-eng-1", "x"]) == {"sw-eng-1": new, "x": "x"}
