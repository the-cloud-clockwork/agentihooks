import io
import json
import subprocess
import urllib.error
from dataclasses import replace

import pytest

from scripts import openbao_kv
from scripts.hive import auth, cli, create, machine, registry, remove
from scripts.hive.placement import choose, place_master
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord

PUBLIC = "redis://hive.example:6380/2"
TARGET = "ssh://worker@10.10.30.207:2222"
SLUG = "demo"
SEAT = f"eng-1@{SLUG}"
PR = "https://github.com/the-cloud-clockwork/antoncore/pull/9"
TREE = [
    "k8s/argocd/apps/agentihooks-hive-anton-9.yaml",
    "k8s/argocd/apps/agentihooks-hive-anton-90.yaml",
    "k8s/manifests/agentihooks-hive-anton-9/hive.yaml",
    "k8s/values-overlays/agentihooks-hive-anton-9/values.yaml",
    "k8s/manifests/agentihooks-hive-anton-2/hive.yaml",
]
OWNED = [TREE[0], TREE[2], TREE[3]]
READS = [
    list(machine.LIST),
    ["gh", "api", "repos/the-cloud-clockwork/antoncore/git/trees/dev?recursive=1", "--jq", remove.TREE],
]


class Swarm:
    def __init__(self, redis):
        self.redis = redis
        self.rows = []

    def slugs(self):
        return [SLUG] if self.rows else []

    def agents(self, slug):
        return list(self.rows) if slug == SLUG else []

    def put_agent(self, slug, agent):
        self.rows = [row for row in self.rows if row.name != agent.name] + [agent]


@pytest.fixture
def store():
    import fakeredis

    return Swarm(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


class Bao:
    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request.get_method(), request.full_url))
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, io.BytesIO(b'{"errors": []}'))
        return io.BytesIO(b"")


class Run:
    def __init__(self, machines=(), tree=()):
        self.machines = list(machines)
        self.tree = list(tree)
        self.calls = []
        self.inputs = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs["timeout"]))
        self.inputs.append(kwargs.get("input"))
        if argv == list(machine.LIST):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.machines), "")
        if argv[:2] == ["herdr", "machine"] and argv[2] == "remove":
            self.machines = [saved for saved in self.machines if saved["id"] != argv[3]]
        if argv[:2] == ["gh", "api"] and argv[2].endswith("?recursive=1"):
            return subprocess.CompletedProcess(argv, 0, "".join(f"{path}\n" for path in self.tree), "")
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 0, f"{PR}\n", "")
        if argv[:3] == ["gh", "pr", "merge"]:
            self.tree = [path for path in self.tree if "anton-9" not in path]
        return subprocess.CompletedProcess(argv, 0, "sha\n" if argv[:2] == ["gh", "api"] else "", "")


class Clock:
    def __init__(self, store, member, leave_after):
        self.store, self.member, self.leave_after = store, member, leave_after
        self.now = 0.0
        self.slept = 0

    def sleep(self, seconds):
        self.slept += 1
        self.now += seconds
        if self.slept == self.leave_after:
            for agent in self.store.agents(SLUG):
                self.store.put_agent(SLUG, replace(agent, state="finished"))

    def __call__(self):
        return self.now


def _member(store, name="anton-9"):
    grant = auth.mint(store.redis, name, PUBLIC)
    member = grant["id"]
    registry.update(store.redis, member, ["ui=no"])
    store.redis.set(f"{registry.key(member)}:sessions", "{}")
    store.redis.hset(f"{registry.key(member)}:join-reports", "e1", "{}")
    return member


def _agent(store, member):
    agent = AgentRecord("engineer@1", "eng", "t1", seat=SEAT, hive=member, state="working")
    store.put_agent(SLUG, agent)


def _host(clock, run, tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("[10.10.30.207]:2222 ssh-ed25519 AAAA\n")
    return remove.Host(run=run, sleep=clock.sleep, clock=clock, known_hosts=known, home=tmp_path)


def _created(store, tmp_path, name="anton-9"):
    store.redis.hset(machine.MACHINES, mapping={"worker-009": name, "worker-001": "swarm-hive-001"})
    (tmp_path / create.STATE_FILE).write_text(json.dumps({"name": name, "finished": True}))


def _hive(hive_id, **fields):
    record = {
        "id": hive_id,
        "ui": "no",
        "ephemeral": "no",
        "excluded": [],
        "prefer": {},
        "max_agents": 5,
        "agents": 0,
        "room": 5,
        "slots": [{"harness": "claude", "account": "a", "kind": "subscription"}],
        "repos": ["r"],
        "interactive": {},
        "master_login": {},
        "heartbeat_at": 1000,
    }
    return {**record, **fields}


def test_a_draining_hive_takes_no_agent_and_no_master():
    hives = [_hive("gone", draining="yes", prefer={"eng": 1}), _hive("kept")]
    assert choose("eng", "claude", "a", "subscription", "r", hives, 1000).hive == "kept"
    assert place_master("claude", "", "r", hives, 1000).hive == "kept"
    assert registry.parse(["draining=yes"]) == {"draining": "yes"}


def _removal(store, tmp_path):
    member = _member(store)
    _agent(store, member)
    _created(store, tmp_path)
    run = Run([{"id": "p7", "label": "anton-9", "target": TARGET}, {"id": "p2", "label": "other", "target": "x"}], TREE)
    bao = openbao_kv.OpenBao("https://bao.example", "t", opener=Bao())
    return member, run, bao, remove.Plan(member, "anton-9", "k8s/agentihooks-hive-anton-9", wait_s=600)


def _written(store, tmp_path):
    keys = {key: store.redis.type(key) for key in store.redis.keys("*")}
    return keys, sorted(store.redis.acl_users()), sorted(path.name for path in tmp_path.iterdir())


def test_remove_runs_every_step_in_order_and_deletes_the_hive_everywhere(store, tmp_path):
    member, run, bao, plan = _removal(store, tmp_path)
    known = str(tmp_path / "known_hosts")

    done = remove.remove(store, bao, plan, _host(Clock(store, member, 2), run, tmp_path))

    assert done == [
        f"drain: drained {member}: 1 agents handed off: engineer@1",
        f"member: revoked member {member} and its Redis user",
        "herdr: removed herdr machine anton-9",
        "herdr: removed the host key of [10.10.30.207]:2222",
        f"registry: deleted 3 registry records of {member}",
        "registry: released the hive name anton-9 of node worker-009",
        "registry: deleted the hive create state of anton-9",
        f"manifests: opened pull request {PR} deleting {', '.join(OWNED)}; it merges once its checks pass",
        "openbao: deleted OpenBao secret/k8s/agentihooks-hive-anton-9",
    ]
    assert [item.text for item in _orders(store, SEAT)] == [remove.ORDER.format(hive=member, slug=SLUG)]
    assert not store.redis.exists(remove.member_key(member))
    assert f"hive-{member}" not in store.redis.acl_users()
    assert member not in store.redis.smembers(registry.INDEX)
    assert not store.redis.keys(f"{registry.key(member)}*")
    assert store.redis.hgetall(machine.MACHINES) == {"worker-001": "swarm-hive-001"}
    assert not (tmp_path / create.STATE_FILE).exists()
    commands = [argv[:4] for argv, _ in run.calls]
    assert commands.index(["herdr", "machine", "remove", "p7"]) < commands.index(["gh", "pr", "create", "-R"])
    assert (["ssh-keygen", "-R", "[10.10.30.207]:2222", "-f", known], machine.SSH_TIMEOUT_S) in run.calls
    tree = next(json.loads(body) for body in run.inputs if body and "base_tree" in body)["tree"]
    assert tree == [{"path": path, "mode": "100644", "type": "blob", "sha": None} for path in OWNED]
    assert bao.opener.requests == [("DELETE", "https://bao.example/v1/secret/metadata/k8s/agentihooks-hive-anton-9")]


def test_a_rerun_after_a_completed_remove_writes_nothing_and_succeeds(store, tmp_path):
    member, run, bao, plan = _removal(store, tmp_path)
    host = _host(Clock(store, member, 2), run, tmp_path)
    remove.remove(store, bao, plan, host)
    bao.opener.status = 404
    before, calls = _written(store, tmp_path), len(run.calls)

    again = remove.remove(store, bao, plan, host)

    assert again == [
        f"drain: drained {member}: 0 agents handed off: none",
        f"member: {member} holds no member credential",
        "herdr: herdr holds no machine anton-9",
        f"registry: deleted 0 registry records of {member}",
        "manifests: the-cloud-clockwork/antoncore dev holds no manifests of agentihooks-hive-anton-9",
        "openbao: deleted OpenBao secret/k8s/agentihooks-hive-anton-9",
    ]
    assert _written(store, tmp_path) == before
    assert [argv for argv, _ in run.calls[calls:]] == READS
    assert len(_orders(store, SEAT)) == 1


def test_a_dry_run_prints_every_step_first_and_writes_nothing(store, tmp_path):
    member, run, bao, plan = _removal(store, tmp_path)
    host = _host(Clock(store, member, 99), run, tmp_path)
    before = _written(store, tmp_path)

    shown = remove.remove(store, bao, replace(plan, dry_run=True), host)

    assert shown == [
        f"drain: would stop placement on {member} and wait up to 600 seconds for engineer@1 to hand off",
        f"member: would revoke member {member} and its Redis user",
        "herdr: would remove herdr machine anton-9 and the host key of [10.10.30.207]:2222",
        f"registry: would delete 3 registry records of {member}",
        "registry: would release the hive name anton-9 of node worker-009",
        "registry: would delete the hive create state of anton-9",
        f"manifests: would open a pull request into the-cloud-clockwork/antoncore dev deleting {', '.join(OWNED)}",
        "openbao: would delete OpenBao secret/k8s/agentihooks-hive-anton-9",
        "dry run of hive remove anton-9: nothing written",
    ]
    assert _written(store, tmp_path) == before
    assert "draining" not in registry.show(store.redis, member)
    assert [argv for argv, _ in run.calls] == READS
    assert bao.opener.requests == []
    assert _orders(store, SEAT) == []


def test_a_hive_whose_agents_stay_is_left_draining_with_its_member(store, tmp_path):
    member = _member(store)
    _agent(store, member)
    store.put_agent(SLUG, AgentRecord("engineer@2", "eng", "t2", seat=f"eng-2@{SLUG}", hive=member))
    plan = remove.Plan(member, "anton-9", "k8s/agentihooks-hive-anton-9", wait_s=30)
    bao = openbao_kv.OpenBao("https://bao.example", "t", opener=Bao())
    clock = Clock(store, member, 99)

    with pytest.raises(auth.HiveError) as refused:
        remove.remove(store, bao, plan, _host(clock, Run(), tmp_path))

    assert str(refused.value) == (
        f"hive {member} takes no new agents, but engineer@1, engineer@2 did not hand off within 30 seconds; "
        "run remove again to keep waiting"
    )
    assert (clock.now, clock.slept) == (30, 2)
    assert registry.show(store.redis, member)["draining"] == "yes"
    assert store.redis.exists(remove.member_key(member))


def test_the_hive_is_named_by_its_member_or_its_name_and_a_gone_name_stands_for_both(store):
    member = _member(store, "anton-7")
    assert remove.resolve(store.redis, member) == (member, "anton-7")
    assert remove.resolve(store.redis, "anton-7") == (member, "anton-7")
    auth.revoke(store.redis, member)
    with pytest.raises(auth.HiveError, match="pass --label"):
        remove.resolve(store.redis, member)
    remove.forget(store.redis, member)
    with pytest.raises(auth.HiveError, match="pass --label"):
        remove.resolve(store.redis, member)
    assert remove.resolve(store.redis, "swarm-hive-004") == ("swarm-hive-004", "swarm-hive-004")


@pytest.mark.parametrize("state", ["starting", "retiring", "launching", "suspect"])
def test_an_agent_in_any_state_but_finished_holds_the_drain(store, tmp_path, state):
    member = _member(store)
    _agent(store, member)
    agent = store.agents(SLUG)[0]
    store.put_agent(SLUG, replace(agent, state=state))
    plan = remove.Plan(member, "anton-9", "k8s/agentihooks-hive-anton-9", wait_s=15)
    bao = openbao_kv.OpenBao("https://bao.example", "t", opener=Bao())

    with pytest.raises(auth.HiveError, match="did not hand off"):
        remove.remove(store, bao, plan, _host(Clock(store, member, 99), Run(), tmp_path))


def test_an_ended_agent_on_the_hive_does_not_hold_the_drain(store):
    member = _member(store)
    _agent(store, member)
    store.put_agent(SLUG, replace(store.agents(SLUG)[0], state="ended"))
    assert remove.hosted(store, member) == []


def _orders(store, seat):
    return [item for inbox in InboxStore(store.redis).worlds() for item in inbox.inbox(seat)]


def test_a_stopping_hive_orders_its_agents_to_hand_off_once_and_ends_when_they_are_gone(store, tmp_path):
    member = _member(store)
    _agent(store, member)
    clock = Clock(store, member, 2)

    settled = remove.settle(store, member, 3600, _host(clock, Run(), tmp_path))

    assert settled == f"hive {member} drained: no agents running, 1 ordered to hand off"
    assert (clock.now, clock.slept) == (2 * remove.POLL_S, 2)
    assert registry.show(store.redis, member)["draining"] == "yes"
    orders = _orders(store, SEAT)
    assert [(item.sender, item.text) for item in orders] == [
        (remove.SENDER, remove.ROLL.format(hive=member, slug=SLUG))
    ]
    assert f"agentihooks swarm {SLUG} handoff" in orders[0].text
    assert "another hive" in orders[0].text
    assert "even while you wait on checks, a merge or a reply" in orders[0].text


def test_a_stopping_hive_with_no_agents_orders_nothing(store, tmp_path):
    member = _member(store)
    clock = Clock(store, member, 99)

    settled = remove.settle(store, member, 3600, _host(clock, Run(), tmp_path))

    assert settled == f"hive {member} drained: no agents running, 0 ordered to hand off"
    assert clock.slept == 0
    assert _orders(store, SEAT) == []


class Successor:
    def __init__(self, store):
        self.store, self.now, self.slept = store, 0.0, 0

    def sleep(self, seconds):
        self.slept += 1
        self.now += seconds
        for agent in self.store.agents(SLUG):
            if _orders(self.store, agent.seat):
                self.store.put_agent(SLUG, replace(agent, state="finished"))

    def __call__(self):
        return self.now


def test_a_release_roll_ends_its_drain_one_poll_after_its_agents_obey_the_order_not_at_the_grace(store, tmp_path):
    member = _member(store)
    _agent(store, member)
    store.put_agent(SLUG, AgentRecord("ci@1", "ci", "t2", seat=f"ci-1@{SLUG}", hive=member, state="working"))
    clock = Successor(store)

    settled = remove.settle(store, member, 3600, _host(clock, Run(), tmp_path))

    assert settled == f"hive {member} drained: no agents running, 2 ordered to hand off"
    assert (clock.now, clock.slept) == (remove.POLL_S, 1)
    assert remove.hosted(store, member) == []


def test_a_stopping_hive_gives_up_on_its_agents_at_the_deadline(store, tmp_path):
    member = _member(store)
    _agent(store, member)
    store.put_agent(SLUG, AgentRecord("engineer@2", "eng", "t2", seat=f"eng-2@{SLUG}", hive=member))
    clock = Clock(store, member, 99)

    settled = remove.settle(store, member, 30, _host(clock, Run(), tmp_path))

    assert settled == f"hive {member} stops after 30 seconds with engineer@1, engineer@2 still running"
    assert (clock.now, clock.slept) == (30, 2)
    assert [len(_orders(store, seat)) for seat in (SEAT, f"eng-2@{SLUG}")] == [1, 1]


def test_a_stopping_hive_with_no_record_is_refused_and_never_recorded(store, tmp_path):
    with pytest.raises(auth.HiveError) as refused:
        remove.settle(store, "pod-name", 30, _host(Clock(store, "pod-name", 1), Run(), tmp_path))

    assert str(refused.value) == "hive pod-name has no record to drain"
    assert registry.show(store.redis, "pod-name") is None


def test_the_drain_command_settles_this_hive_with_its_wait(store, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: store.redis)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "anton-9")
    seen = []

    def settled(swarm, hive, wait_s, host):
        seen.append((swarm.redis is store.redis, hive, wait_s, type(host)))
        return "drained"

    monkeypatch.setattr(remove, "settle", settled)

    assert cli.main(["drain"]) == 0
    assert cli.main(["drain", "--wait", "5"]) == 0
    assert capsys.readouterr().out == "drained\ndrained\n"
    assert seen == [(True, "anton-9", 3600, remove.Host), (True, "anton-9", 5, remove.Host)]


def test_the_drain_help_names_its_wait(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.main(["drain", "--help"])
    shown = capsys.readouterr().out
    assert shown.startswith("usage: agentihooks hive drain [-h] [--wait WAIT]")
    assert " seconds to wait for its agents to finish\n" in shown
    assert " Take no new agents on this hive and wait until its agents finish\n" in cli._parser().format_help()


@pytest.mark.parametrize(
    ("target", "host"),
    [(TARGET, "[10.10.30.207]:2222"), ("ssh://worker@box:22", "box"), ("worker@box", "box")],
)
def test_the_known_host_entry_names_a_non_default_port(target, host):
    assert machine.known_host(target) == host


def test_an_openbao_failure_other_than_missing_is_raised():
    bao = openbao_kv.OpenBao("https://bao.example", "t", opener=Bao(503))
    with pytest.raises(openbao_kv.OpenBaoError):
        openbao_kv.delete(bao, "k8s/x")
    with pytest.raises(auth.HiveError) as refused:
        remove.unbank(bao, "k8s/x")
    assert str(refused.value) == "OpenBao HTTP 503: x"


def test_the_remove_command_refuses_without_openbao(monkeypatch, capsys):
    monkeypatch.delenv(openbao_kv.URL_ENV, raising=False)
    monkeypatch.delenv(openbao_kv.TOKEN_ENV, raising=False)
    assert cli.main(["remove", "abc"]) == 1
    assert "OPENBAO_URL" in capsys.readouterr().err


def test_the_remove_command_builds_its_plan_from_the_member_and_its_options(store, monkeypatch, capsys):
    member = _member(store)
    monkeypatch.setattr(cli, "redis_client", lambda: store.redis)
    monkeypatch.setenv(openbao_kv.URL_ENV, "https://bao.example")
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, "t")
    seen = []

    def removed(swarm, bao, plan, host):
        seen.append((swarm.redis is store.redis, bao, plan, type(host)))
        return ["first", "second"]

    monkeypatch.setattr(remove, "remove", removed)

    assert cli.main(["remove", member]) == 0
    assert capsys.readouterr().out == "first\nsecond\n"
    assert cli.main(["remove", "anton-9", "--dry-run"]) == 0
    given = ["--label", "l", "--path", "p", "--repo", "o/r", "--base", "main2", "--wait", "5"]
    assert cli.main(["remove", member, *given]) == 0
    assert cli.main(["remove", "anton-9", "--label", "l"]) == 0
    bao = openbao_kv.OpenBao("https://bao.example", "t")
    assert seen == [
        (True, bao, remove.Plan(member, "anton-9", "k8s/agentihooks-hive-anton-9"), remove.Host),
        (True, bao, remove.Plan(member, "anton-9", "k8s/agentihooks-hive-anton-9", dry_run=True), remove.Host),
        (True, bao, remove.Plan(member, "l", "p", "o/r", "main2", 5), remove.Host),
        (True, bao, remove.Plan(member, "l", "k8s/agentihooks-hive-l"), remove.Host),
    ]
    monkeypatch.delenv(openbao_kv.TOKEN_ENV)
    assert cli.main(["remove", member, "--dry-run"]) == 0
    assert seen[-1][1] is None


def test_the_remove_help_names_every_option(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.main(["remove", "--help"])
    shown = capsys.readouterr().out
    assert shown.startswith(
        "usage: agentihooks hive remove [-h] [--label LABEL] [--path PATH] [--repo REPO] [--base BASE] [--wait WAIT] "
        "[--dry-run] NAME"
    )
    for text in (
        "hive name or member id",
        "herdr label and manifest name; defaults to the member's name",
        "OpenBao record under the secret mount; defaults to k8s/agentihooks-hive-LABEL",
        "repository holding the hive manifests; defaults to the-cloud-clockwork/antoncore",
        "branch the removal pull request merges into; defaults to dev",
        "seconds to wait for its agents to hand off",
        "Print every step, and write nothing",
    ):
        assert f" {text}\n" in shown
    assert " Drain a hive, then delete it from Redis, herdr, known hosts, OpenBao and antoncore\n" in (
        cli._parser().format_help()
    )
