import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.swarm.store as swarm_store
from scripts.hive import auth, cli, create, create_github, create_render, invite, machine, nats_grant, registry
from tests.hive.test_invite import CA, LIKE, SWARM, Bao, _bao, _credential

pytestmark = pytest.mark.unit

NOW = 1_800_000_000
NAME = "swarm-hive-001"
RECORD = f"k8s/agentihooks-hive-{NAME}"
REPO = "the-cloud-clockwork/antoncore"
PR = "https://github.com/the-cloud-clockwork/antoncore/pull/2000"
TEMPLATE = (
    "#@ file: k8s/manifests/${name}/ssh-lan.yaml\nport: ${ssh_port}\nnode: ${node}\nkey: ${record}\nip: ${lan_ip}\n"
)
SERVICES = {
    "items": [
        {
            "metadata": {
                "annotations": {
                    create_render.LAN_IP_ANNOTATION: "10.10.30.207",
                    create_render.SHARED_IP_ANNOTATION: create_render.SHARED_IP_KEY,
                }
            },
            "spec": {"ports": [{"port": 2222}, {"port": 2223}, {"port": 2224}]},
        }
    ]
}
TARGET = "ssh://worker@10.10.30.207:2225"
SIGNER = "agentihooks/hub/nats-signer"
SYNC = "agentihooks-swarm-nats-signer"
RESYNC = [
    "kubectl",
    "-n",
    "agentihooks-swarm",
    "annotate",
    "externalsecret",
    SYNC,
    f"force-sync={NOW * 1000}",
    "--overwrite",
]


class Run:
    def __init__(self):
        self.calls = []
        self.cwds = []
        self.state = "OPEN"
        self.prs = []
        self.on_merge = lambda: None
        self.merge_error = ""
        self.checks = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.cwds.append(kwargs.get("cwd"))
        answer = self._answer(argv, kwargs.get("input"))
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        return subprocess.CompletedProcess(argv, 0, answer, "")

    def _answer(self, argv, stdin):
        if argv[:3] == ["kubectl", "get", "service"]:
            return json.dumps(SERVICES)
        if argv[0] == "ssh-keygen":
            path = Path(argv[argv.index("-f") + 1])
            path.write_text("PRIVATE HOST KEY\n")
            path.with_suffix(".pub").write_text("ssh-ed25519 HOSTPUBLIC swarm\n")
            return ""
        if argv[:3] == ["herdr", "machine", "list"]:
            return json.dumps(getattr(self, "machines", []))
        if argv[0] != "gh":
            return ""
        return self._gh(argv[1:], stdin)

    def _gh(self, argv, stdin):
        if argv[:2] == ["api", "-H"]:
            assert f"contents/{create_render.TEMPLATE_PATH}?" in argv[3]
            return TEMPLATE
        if argv[:2] == ["api", f"repos/{REPO}/git/trees/dev?recursive=1"]:
            assert argv[2:] == ["--jq", create_github.TREE]
            return "\n".join(getattr(self, "tree", []))
        if argv[:2] == ["api", "repos/the-cloud-clockwork/antoncore/rules/branches/dev"]:
            return json.dumps(getattr(self, "required", []))
        if argv[:2] == ["pr", "list"] and "open" in argv:
            return "\n".join(getattr(self, "open_heads", []))
        if argv[:2] == ["pr", "list"]:
            return self.prs[0] if self.prs else ""
        if argv[:2] == ["pr", "create"]:
            self.prs.append(PR)
            return PR
        if argv[:2] == ["pr", "view"] and "statusCheckRollup" in argv:
            return json.dumps({"statusCheckRollup": self.checks.pop(0)})
        if argv[:2] == ["pr", "view"]:
            return self.state
        if argv[:2] == ["pr", "merge"] and "--auto" in argv and self.merge_error:
            return subprocess.CompletedProcess(argv, 1, "", self.merge_error)
        if argv[:2] == ["pr", "merge"]:
            self.state = "MERGED"
            self.on_merge()
            return ""
        if argv[:3] == ["api", "-X", "POST"]:
            self.posted = getattr(self, "posted", []) + [(argv[3], json.loads(stdin))]
        return "sha"

    def named(self, *prefix):
        return [argv for argv in self.calls if argv[: len(prefix)] == list(prefix)]


class Store:
    working = True
    placing = True
    swarm_state = "running"

    def __init__(self, redis):
        self.redis = redis

    def slugs(self):
        return ["demo"]

    def config(self, slug):
        return SimpleNamespace(hive_placement=Store.placing, state=Store.swarm_state)

    def agents(self, slug):
        hives = [
            key.split(":")[-1] for key in self.redis.keys(registry.key("*")) if self.redis.hget(key, "heartbeat_at")
        ]
        state = "working" if Store.working else "starting"
        return [SimpleNamespace(name=f"engineer@{hive}", hive=hive, lane="eng", state=state) for hive in hives]


@pytest.fixture
def world(tmp_path, monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    registry.update(redis, "root", ["ui=yes"])
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 OPERATOR operator\n")
    bao = Bao(
        {
            LIKE: {"credential": _credential(), "swarm_url": SWARM, "ca_pem": CA},
            SIGNER: {"member_swarms": {"old": ["demo"]}},
        }
    )
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / "agentihooks"),
        nats_grant.SIGNER_ENV: SIGNER,
    }
    run, lines = Run(), []
    published = []
    monkeypatch.setattr(create.setup, "publish", lambda env, path, dry: published.append(dry) or f"settings {dry}")
    monkeypatch.setattr(swarm_store, "RedisStore", Store)
    Store.working = Store.placing = True
    Store.swarm_state = "running"

    def beat():
        member = invite._env(bao.records[RECORD]["credential"])[invite.HIVE_ID]
        redis.hset(registry.key(member), "heartbeat_at", NOW * 1000)

    run.on_merge = beat
    now = [NOW]
    tools = create.Tools(
        redis,
        _bao(bao),
        environ,
        "root",
        "redis://127.0.0.1:6379/0",
        run=run,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        out=lines.append,
    )
    return SimpleNamespace(tools=tools, bao=bao, run=run, lines=lines, home=tmp_path, published=published)


def _state(world):
    return json.loads((world.home / "agentihooks" / create.STATE_FILE).read_text())


def test_create_brings_a_named_hive_from_nothing_to_an_engineer_in_herdr(world):
    done = create.create(world.tools, create.Options(timeout_s=60))

    assert done == f"hive {NAME} is live in herdr as {TARGET}"
    record = world.bao.records[RECORD]
    member = invite._env(record["credential"])[invite.HIVE_ID]
    assert record["swarm_url"] == SWARM and record["ca_pem"] == CA
    assert record[create.HOST_KEY] == "PRIVATE HOST KEY\n"
    assert record[create.AUTHORIZED_KEYS] == "ssh-ed25519 OPERATOR operator\n"
    tree = dict(world.run.posted)[f"repos/{REPO}/git/trees"]["tree"]
    assert {entry["path"]: entry["content"] for entry in tree} == {
        f"k8s/manifests/{NAME}/ssh-lan.yaml": f"port: 2225\nnode: worker-001\nkey: {RECORD}\nip: 10.10.30.207\n",
    }
    assert dict(world.run.posted)[f"repos/{REPO}/git/refs"]["ref"] == f"refs/heads/{NAME}"
    assert world.run.named("gh", "pr", "merge") == [["gh", "pr", "merge", PR, "--squash", "--auto"]]
    assert world.published == [False]
    known = (world.home / ".ssh" / "known_hosts").read_text()
    assert known == "[10.10.30.207]:2225 ssh-ed25519 HOSTPUBLIC swarm\n"
    herdr = world.run.named("herdr", "machine", "add")
    assert herdr == [["herdr", "machine", "add", "--label", NAME, "--remote-session", "default", TARGET]]
    state = _state(world)
    assert state["finished"] is True and state["pr"] == PR and state["ssh_port"] == 2225
    assert state["added"] == member
    assert world.run.named("ssh") == []
    assert world.tools.redis.keys(f"{auth.PREFIX}:member:*") == [f"{auth.PREFIX}:member:{member}"]
    titles = [line.split(": ")[1] for line in world.lines]
    assert titles == [title for title, _ in create.STEPS]
    assert world.lines[-1] == f"hive create {NAME}: engineer: engineer@{member} in demo works on the hive"


def test_the_manifests_step_opens_the_pull_request_through_its_clone_outside_a_git_checkout(world, monkeypatch):
    outside = world.home / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    create.create(world.tools, create.Options(timeout_s=60))

    (init,) = world.run.named("git", "init")
    clone = init[-1]
    assert init == ["git", "init", "-q", clone]
    origin = ["git", "-C", clone, "remote", "add", "origin", f"https://github.com/{REPO}.git"]
    assert world.run.named("git", "-C") == [origin]
    calls = list(zip(world.run.calls, world.run.cwds))
    first = world.run.calls.index(init)
    last = world.run.calls.index(world.run.named("gh", "pr", "create")[0])
    step = calls[first + 2 : last + 1]
    assert [argv[:2] for argv, _ in step] == [
        ["gh", "api"],
        ["gh", "pr"],
        ["gh", "api"],
        ["gh", "api"],
        ["gh", "api"],
        ["gh", "api"],
        ["gh", "api"],
        ["gh", "pr"],
    ]
    assert {cwd for _, cwd in step} == {clone}
    assert not Path(clone).exists()
    assert _state(world)["pr"] == PR


def test_the_dry_run_lists_every_manifest_file(world, monkeypatch):
    monkeypatch.setattr(create, "_files", lambda tools, options, cwd: {"a.yaml": "a", "b.yaml": "b"})
    create.create(world.tools, create.Options(dry_run=True))
    assert (
        f"hive create {NAME}: manifests: would open a pull request into {REPO} dev with a.yaml, b.yaml" in world.lines
    )


def test_a_rerun_resumes_the_unfinished_create_and_repeats_no_finished_step(world):
    Store.working = False
    with pytest.raises(auth.HiveError, match="waiting for an engineer working on the hive; rerun hive create"):
        create.create(world.tools, create.Options(timeout_s=0))
    first = dict(world.bao.records[RECORD])
    calls = len(world.run.calls)
    Store.working = True

    assert create.create(world.tools, create.Options(timeout_s=0)) == f"hive {NAME} is live in herdr as {TARGET}"
    assert world.bao.records[RECORD] == first
    later = world.run.calls[calls:]
    assert not [argv for argv in later if argv[:3] in (["gh", "pr", "create"], ["herdr", "machine", "add"])]
    assert not [argv for argv in later if argv[0] in ("ssh-keygen", "ssh", "kubectl")]
    assert world.lines[len(create.STEPS) - 1] == f"hive create {NAME}: resuming the unfinished create"
    assert f"hive create {NAME}: manifests: kept pull request {PR}" in world.lines
    assert (world.home / ".ssh" / "known_hosts").read_text().count("HOSTPUBLIC") == 1


def test_a_finished_create_names_the_next_hive_on_the_next_run(world):
    create.create(world.tools, create.Options(timeout_s=60))
    calls = len(world.run.calls)

    assert create.create(world.tools, create.Options(dry_run=True)) == (
        "dry run of hive create swarm-hive-002: nothing written"
    )
    later = world.run.calls[calls:]
    assert not [argv for argv in later if argv[:3] in (["gh", "pr", "create"], ["herdr", "machine", "add"])]
    assert _state(world)["name"] == NAME and _state(world)["finished"] is True
    assert len(world.tools.redis.keys(f"{auth.PREFIX}:member:*")) == 1


def test_a_second_create_that_stops_early_is_saved_unfinished_under_the_next_name(world):
    create.create(world.tools, create.Options(timeout_s=60))
    world.run.prs = []

    with pytest.raises(auth.HiveError, match="rerun hive create to resume$"):
        create.create(world.tools, create.Options(timeout_s=0))
    assert _state(world)["name"] == "swarm-hive-002" and "finished" not in _state(world)


def test_a_create_without_its_saved_state_names_a_new_hive_beside_the_one_it_finished(world):
    create.create(world.tools, create.Options(timeout_s=60))
    (world.home / "agentihooks" / create.STATE_FILE).unlink()
    world.run.machines = [{"id": "a1", "label": NAME, "target": TARGET}]

    assert create.create(world.tools, create.Options(dry_run=True)) == (
        "dry run of hive create swarm-hive-002: nothing written"
    )
    assert len(world.tools.redis.keys(f"{auth.PREFIX}:member:*")) == 1


def test_a_create_on_another_node_names_the_next_hive(world):
    create.create(world.tools, create.Options(timeout_s=60))
    world.run.prs = []

    with pytest.raises(auth.HiveError):
        create.create(world.tools, create.Options(node="worker-002", timeout_s=0))
    assert _state(world)["name"] == "swarm-hive-002" and _state(world)["node"] == "worker-002"
    assert world.tools.redis.hgetall(machine.MACHINES) == {}


def test_a_create_without_its_saved_state_keeps_the_port_of_the_hive_service(world, monkeypatch):
    service = {
        "metadata": {
            "name": f"{NAME}-ssh-lan",
            "namespace": NAME,
            "annotations": {create_render.LAN_IP_ANNOTATION: "10.10.30.207"},
        },
        "spec": {"ports": [{"port": 2223}]},
    }
    monkeypatch.setitem(SERVICES, "items", [*SERVICES["items"], service])

    create.create(world.tools, create.Options(dry_run=True))

    assert (
        f"--- k8s/manifests/{NAME}/ssh-lan.yaml\nport: 2223\nnode: worker-001\nkey: {RECORD}\nip: 10.10.30.207\n"
        in world.lines
    )


def test_the_dry_run_prints_every_step_and_file_and_writes_nothing(world):
    done = create.create(world.tools, create.Options(dry_run=True))

    assert done == f"dry run of hive create {NAME}: nothing written"
    assert world.bao.written == []
    assert world.tools.redis.hgetall(machine.MACHINES) == {}
    assert not (world.home / "agentihooks" / create.STATE_FILE).exists()
    assert not (world.home / ".ssh" / "known_hosts").exists()
    assert not world.run.named("gh", "api", "-X", "POST")
    assert not world.run.named("gh", "pr", "create")
    writes = [argv for argv in world.run.calls if argv != list(machine.LIST)]
    assert not [argv for argv in writes if argv[0] in ("ssh-keygen", "herdr", "ssh")]
    assert world.published == [True]
    assert (
        f"--- k8s/manifests/{NAME}/ssh-lan.yaml\nport: 2225\nnode: worker-001\nkey: {RECORD}\nip: 10.10.30.207\n"
        in world.lines
    )
    assert [line for line in world.lines if line.startswith("--- ")] == [
        f"--- k8s/manifests/{NAME}/ssh-lan.yaml\nport: 2225\nnode: worker-001\nkey: {RECORD}\nip: 10.10.30.207\n"
    ]
    steps = [line for line in world.lines if line.startswith(f"hive create {NAME}: ")]
    assert [line.split(": ")[1] for line in steps] == [title for title, _ in create.STEPS]
    assert all(": would " in line for line in steps if ": settings: " not in line)


def test_no_public_key_is_a_refusal_before_any_key_is_written(world):
    (world.home / ".ssh" / "id_ed25519.pub").unlink()
    with pytest.raises(auth.HiveError, match="no public key under ~/.ssh"):
        create.create(world.tools, create.Options(timeout_s=0))
    assert create.HOST_KEY not in world.bao.records[RECORD]


def test_an_existing_pull_request_for_the_branch_is_reused():
    run = Run()
    run.prs = [PR]
    assert create_github.open_pr(run, REPO, "dev", NAME, {"a": "b"}, "Add worker hive") == PR
    assert not run.named("gh", "api")


def test_a_failing_gh_names_its_command():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")

    with pytest.raises(auth.HiveError, match="gh pr view exited 1: HTTP 404"):
        create_github.pr_state(run, PR)


def test_cli_create_needs_openbao(monkeypatch, capsys):
    monkeypatch.delenv("OPENBAO_URL", raising=False)
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)
    assert cli.main(["create", "--dry-run"]) == 1
    assert "hive create refused: OPENBAO_URL and OPENBAO_TOKEN must both be set" in capsys.readouterr().err


def test_cli_create_passes_its_options(monkeypatch, capsys):
    seen = {}
    monkeypatch.setenv("OPENBAO_URL", "https://bao.test")
    monkeypatch.setenv("OPENBAO_TOKEN", "token")
    monkeypatch.setattr(cli, "redis_client", lambda: None)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    monkeypatch.setattr(create, "create", lambda tools, options: seen.update(options=options) or "created")
    argv = ["create", "--node", "worker-002", "--timeout", "9", "--dry-run", "--like", "k8s/x", "--repo", "o/r"]
    assert cli.main([*argv, "--base", "main"]) == 0
    assert capsys.readouterr().out == "created\n"
    assert seen["options"] == create.Options("o/r", "main", "k8s/x", "worker-002", 9, True)


def test_a_crash_in_the_first_step_resumes_the_same_hive(world, monkeypatch):
    real = invite.invite

    def crash(*args):
        real(*args)
        raise auth.HiveError("killed")

    monkeypatch.setattr(invite, "invite", crash)
    with pytest.raises(auth.HiveError, match="killed"):
        create.create(world.tools, create.Options(timeout_s=60))
    monkeypatch.setattr(invite, "invite", real)
    first = world.bao.records[RECORD]["credential"]
    world.tools.clock = lambda: NOW + 7
    Store.working = False

    with pytest.raises(auth.HiveError, match="waiting for an engineer"):
        create.create(world.tools, create.Options(timeout_s=0))
    assert _state(world)["name"] == NAME
    assert world.bao.records[RECORD]["credential"] == first


@pytest.mark.parametrize("dry_run", [True, False])
def test_no_swarm_placing_on_hives_is_a_refusal_before_anything_is_named(world, dry_run):
    Store.placing = False
    with pytest.raises(auth.HiveError, match="set hive-placement=on"):
        create.create(world.tools, create.Options(dry_run=dry_run))
    assert world.run.calls == []
    assert not (world.home / "agentihooks" / create.STATE_FILE).exists()


def test_a_closed_pull_request_is_not_reused():
    run = Run()
    create_github.open_pr(run, REPO, "dev", NAME, {"a": "b"}, "Add worker hive")
    listed = run.named("gh", "pr", "list")[0]
    assert listed[listed.index("--jq") + 1] == '[.[] | select(.state != "CLOSED")][0].url // ""'


def test_a_file_mapped_to_none_is_deleted_from_the_tree():
    bodies = []

    def run(argv, **kwargs):
        bodies.append(kwargs.get("input"))
        return subprocess.CompletedProcess(argv, 0, "" if argv[1:3] == ["pr", "list"] else "sha", "")

    create_github.open_pr(run, REPO, "dev", NAME, {"gone": None, "a": "b"}, "Remove worker hive")
    tree = next(json.loads(body) for body in bodies if body and "base_tree" in body)["tree"]
    assert tree == [
        {"path": "gone", "mode": "100644", "type": "blob", "sha": None},
        {"path": "a", "mode": "100644", "type": "blob", "content": "b"},
    ]


def test_a_branch_left_by_an_earlier_run_is_moved_to_the_new_commit():
    run = Run()

    def answer(argv, **kwargs):
        if argv[:5] == ["gh", "api", "-X", "POST", f"repos/{REPO}/git/refs"]:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Reference already exists (HTTP 422)")
        return run(argv, **kwargs)

    assert create_github.open_pr(answer, REPO, "dev", NAME, {"a": "b"}, "Add worker hive", "clone") == PR
    patched = run.named("gh", "api", "-X", "PATCH")
    assert patched == [
        ["gh", "api", "-X", "PATCH", f"repos/{REPO}/git/refs/heads/{NAME}", "--input", "-", "--jq", ".object.sha"]
    ]
    assert set(run.cwds) == {"clone"}


def test_gh_and_the_clone_run_with_their_folder_and_timeout(tmp_path):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "out\n", "")

    folder = str(tmp_path)
    assert create_github.clone(run, REPO, folder) == folder
    assert create_github.gh(run, ["api", "x"], {"a": 1}, folder) == "out"
    common = {
        "input": None,
        "capture_output": True,
        "text": True,
        "timeout": create_github.GH_TIMEOUT_S,
        "check": False,
    }
    origin = ["git", "-C", folder, "remote", "add", "origin", f"https://github.com/{REPO}.git"]
    assert seen == [
        (["git", "init", "-q", folder], common),
        (origin, common),
        (["gh", "api", "x"], {**common, "input": '{"a": 1}', "cwd": folder}),
    ]


def test_another_branch_error_is_a_refusal():
    def answer(argv, **kwargs):
        if argv[:4] == ["gh", "api", "-X", "POST"] and argv[4].endswith("/git/refs"):
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
        return Run()(argv, **kwargs)

    with pytest.raises(auth.HiveError, match="HTTP 403"):
        create_github.open_pr(answer, REPO, "dev", NAME, {"a": "b"}, "Add worker hive")


def test_a_saved_pull_request_closed_without_merging_is_dropped_for_a_new_one(world):
    world.run.state = "CLOSED"
    world.run.prs = [PR]
    world.tools.state = {}
    (world.home / "agentihooks").mkdir()
    saved = {"name": NAME, "record": RECORD, "ssh_port": 2225, "lan_ip": "10.10.30.207", "node": "worker-001", "pr": PR}
    (world.home / "agentihooks" / create.STATE_FILE).write_text(json.dumps(saved))

    with pytest.raises(auth.HiveError, match="was closed without merging; rerun hive create to open a new one"):
        create.create(world.tools, create.Options(timeout_s=0))
    assert "pr" not in _state(world)
    assert not world.run.named("gh", "pr", "merge")


def test_cli_create_defaults_are_the_options_defaults(monkeypatch, capsys):
    seen = {}
    monkeypatch.setenv("OPENBAO_URL", "https://bao.test")
    monkeypatch.setenv("OPENBAO_TOKEN", "token")
    monkeypatch.setattr(cli, "redis_client", lambda: None)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    monkeypatch.setattr(create, "create", lambda tools, options: seen.update(options=options) or "created")
    assert cli.main(["create"]) == 0
    assert seen["options"] == create.Options()


@pytest.mark.parametrize("signer", [None, "file:/nats-signer", "k8s/nats-signer"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_create_without_the_hub_signer_record_refuses_before_anything_is_named(world, signer, dry_run):
    del world.tools.environ[nats_grant.SIGNER_ENV]
    if signer:
        world.tools.environ[nats_grant.SIGNER_ENV] = signer
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(dry_run=dry_run))
    assert str(refused.value) == (
        "hive create lists the new hive member on the hub NATS signer record, "
        "so AGENTIHOOKS_NATS_SIGNER must name an OpenBao record under agentihooks/hub/"
    )
    assert world.run.calls == []
    assert world.bao.written == []
    assert not (world.home / "agentihooks" / create.STATE_FILE).exists()


def _listing_line(world):
    return next(line for line in world.lines if line.startswith(f"hive create {NAME}: invite: ")).rsplit("; ", 1)[1]


def _keep_member(world):
    state = {"name": NAME, "record": RECORD, "ssh_port": 2225, "lan_ip": "10.10.30.207", "node": "worker-001"}
    (world.home / "agentihooks").mkdir(exist_ok=True)
    (world.home / "agentihooks" / create.STATE_FILE).write_text(json.dumps(state))
    grant = auth.mint(world.tools.redis, NAME, "redis://127.0.0.1:6379/0")
    world.bao.records[RECORD] = {"credential": auth.env_text("https://hive.example:8770", grant)}
    world.bao.versions[RECORD] = 1
    return grant["id"]


LISTED = (
    "listed {} on the hub signer record " + SIGNER + " for demo and asked the hub's " + SYNC + " sync to refresh it now"
)


def test_create_lists_the_new_member_and_its_swarms_and_resyncs_the_hub(world):
    create.create(world.tools, create.Options(timeout_s=60))

    member = invite._env(world.bao.records[RECORD]["credential"])[invite.HIVE_ID]
    assert world.bao.records[SIGNER]["member_swarms"] == {"old": ["demo"], member: ["demo"]}
    assert invite._env(world.bao.records[RECORD]["credential"])["AGENTIHOOKS_BUS_SCOPED"] == "1"
    assert world.run.named("kubectl", "-n") == [RESYNC]
    assert world.run.calls.index(RESYNC) < world.run.calls.index(world.run.named("ssh-keygen")[0])
    assert _listing_line(world) == LISTED.format(member)


def test_a_kept_member_left_unlisted_is_listed_on_the_rerun(world):
    member = _keep_member(world)

    create.create(world.tools, create.Options(timeout_s=60))

    assert world.bao.records[SIGNER]["member_swarms"] == {"old": ["demo"], member: ["demo"]}
    assert world.run.named("kubectl", "-n") == [RESYNC]
    assert world.lines[:2] == [
        f"hive create {NAME}: resuming the unfinished create",
        f"hive create {NAME}: invite: kept hive member {member} in OpenBao secret/{RECORD}; {LISTED.format(member)}",
    ]


def test_a_kept_member_that_was_revoked_is_replaced_by_a_new_listed_member(world):
    world.bao.records[RECORD] = {"credential": _credential("feedfacefeedface")}
    world.bao.versions[RECORD] = 1

    create.create(world.tools, create.Options(timeout_s=60))

    member = invite._env(world.bao.records[RECORD]["credential"])[invite.HIVE_ID]
    assert member != "feedfacefeedface"
    assert world.bao.records[SIGNER]["member_swarms"] == {"old": ["demo"], member: ["demo"]}
    assert _listing_line(world) == LISTED.format(member)


def test_a_rerun_after_a_crash_before_the_resync_resyncs_the_listed_member(world, monkeypatch):
    real = create._kubectl

    def crash(tools, argv):
        if argv[:1] == ["-n"]:
            raise auth.HiveError("killed")
        return real(tools, argv)

    monkeypatch.setattr(create, "_kubectl", crash)
    with pytest.raises(auth.HiveError, match="killed"):
        create.create(world.tools, create.Options(timeout_s=60))
    member = invite._env(world.bao.records[RECORD]["credential"])[invite.HIVE_ID]
    assert world.bao.records[SIGNER]["member_swarms"] == {"old": ["demo"], member: ["demo"]}
    monkeypatch.setattr(create, "_kubectl", real)

    create.create(world.tools, create.Options(timeout_s=60))

    assert world.run.named("kubectl", "-n") == [RESYNC]
    assert _state(world)["resynced"] == member
    assert world.lines[:2] == [
        f"hive create {NAME}: resuming the unfinished create",
        f"hive create {NAME}: invite: kept hive member {member} in OpenBao secret/{RECORD}; {LISTED.format(member)}",
    ]


def test_a_finished_listing_is_not_resynced_on_the_rerun(world):
    Store.working = False
    with pytest.raises(auth.HiveError, match="waiting for an engineer"):
        create.create(world.tools, create.Options(timeout_s=0))
    member = _state(world)["resynced"]
    Store.working = True

    create.create(world.tools, create.Options(timeout_s=0))

    assert world.run.named("kubectl", "-n") == [RESYNC]
    assert world.lines[len(create.STEPS)] == (
        f"hive create {NAME}: invite: kept hive member {member} in OpenBao secret/{RECORD}; "
        f"kept {member} on the hub signer record {SIGNER}"
    )


def test_a_hub_record_without_member_swarms_scopes_no_member(world):
    world.bao.records[SIGNER] = {"url": "nats://hub"}

    create.create(world.tools, create.Options(timeout_s=60))

    assert world.bao.records[SIGNER] == {"url": "nats://hub"}
    assert world.run.named("kubectl", "-n") == []
    assert _listing_line(world) == f"the hub signer record {SIGNER} lists no member swarms, so the hub scopes no member"


def test_the_dry_run_names_the_listing_and_writes_no_hub_record(world):
    create.create(world.tools, create.Options(dry_run=True))

    assert world.bao.written == []
    assert world.run.named("kubectl", "-n") == []
    assert world.lines[0] == (
        f"hive create {NAME}: invite: would mint a hive member into OpenBao secret/{RECORD} like {LIKE}; "
        f"would list the new member on the hub signer record {SIGNER} for demo and refresh {SYNC}"
    )


def test_the_listing_names_every_swarm_it_grants(world, monkeypatch):
    monkeypatch.setattr(Store, "slugs", lambda self: ["demo", "rig", "third"])

    create.create(world.tools, create.Options(dry_run=True))

    assert _listing_line(world) == (
        f"would list the new member on the hub signer record {SIGNER} for demo, rig, third and refresh {SYNC}"
    )


def test_the_dry_run_of_a_hub_record_without_member_swarms_promises_no_listing(world):
    world.bao.records[SIGNER] = {"url": "nats://hub"}

    create.create(world.tools, create.Options(dry_run=True))

    assert _listing_line(world) == f"the hub signer record {SIGNER} lists no member swarms, so the hub scopes no member"


def test_the_dry_run_of_a_kept_unlisted_member_names_its_listing(world):
    member = _keep_member(world)

    create.create(world.tools, create.Options(dry_run=True))

    assert world.lines[1] == (
        f"hive create {NAME}: invite: kept hive member {member} in OpenBao secret/{RECORD}; "
        f"would list {member} on the hub signer record {SIGNER} for demo and refresh {SYNC}"
    )


def test_a_new_node_beside_live_hives_one_and_two_is_named_hive_three(world):
    redis = world.tools.redis
    redis.hset(machine.MACHINES, "worker-001", NAME)
    for name in (NAME, "swarm-hive-002"):
        member = auth.mint(redis, name, "redis://hive.example:6380/2")["id"]
        redis.hset(registry.key(member), "heartbeat_at", NOW * 1000)
    world.run.machines = [
        {"id": "a1", "label": NAME, "target": "ssh://worker@10.10.30.207:2222"},
        {"id": "a2", "label": "swarm-hive-002", "target": "ssh://worker@10.10.30.207:2223"},
    ]
    world.run.on_merge = lambda: None

    with pytest.raises(
        auth.HiveError,
        match=r"^timed out after 0s waiting for hive [0-9a-f]{16} to send a heartbeat; rerun hive create to resume$",
    ):
        create.create(world.tools, create.Options(node="worker-002", timeout_s=0))

    assert _state(world)["name"] == "swarm-hive-003"
    assert redis.hgetall(machine.MACHINES) == {"worker-001": NAME}


@pytest.mark.parametrize(
    ("machines", "registered"),
    [
        ([{"id": "a2", "label": "swarm-hive-002", "target": "ssh://worker@10.10.30.207:2223"}], "anton-2"),
        ([], "swarm-hive-002"),
    ],
)
def test_a_name_only_herdr_or_the_registry_holds_is_skipped(world, machines, registered):
    world.tools.redis.hset(machine.MACHINES, "worker-001", NAME)
    world.run.machines = machines
    registry.update(world.tools.redis, "abc", [f"name={registered}"])

    done = create.create(world.tools, create.Options(node="worker-002", dry_run=True))

    assert done == "dry run of hive create swarm-hive-003: nothing written"


def test_a_name_only_a_hive_member_holds_is_skipped(world):
    world.tools.redis.hset(machine.MACHINES, "worker-001", NAME)
    auth.mint(world.tools.redis, "swarm-hive-002", "redis://hive.example:6380/2")

    done = create.create(world.tools, create.Options(node="worker-002", dry_run=True))

    assert done == "dry run of hive create swarm-hive-003: nothing written"


def _live_hive_one(world):
    redis = world.tools.redis
    redis.hset(machine.MACHINES, "worker-001", NAME)
    member = auth.mint(redis, NAME, "redis://hive.example:6380/2")["id"]
    redis.hset(registry.key(member), "heartbeat_at", NOW * 1000)
    world.run.tree = [f"k8s/manifests/{NAME}/ssh-lan.yaml", "k8s/manifests/other/ssh-lan.yaml"]


def test_a_create_on_the_node_of_a_live_hive_names_a_fresh_hive_and_leaves_the_live_one(world):
    _live_hive_one(world)

    done = create.create(world.tools, create.Options(node="worker-001", dry_run=True))

    assert done == "dry run of hive create swarm-hive-002: nothing written"
    assert [line for line in world.lines if line.startswith("--- ")] == [
        "--- k8s/manifests/swarm-hive-002/ssh-lan.yaml\nport: 2225\nnode: worker-001\n"
        "key: k8s/agentihooks-hive-swarm-hive-002\nip: 10.10.30.207\n"
    ]
    assert world.tools.redis.hgetall(machine.MACHINES) == {"worker-001": NAME}


def test_an_explicit_name_of_a_live_hive_with_an_antoncore_overlay_is_refused(world):
    _live_hive_one(world)

    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(name=NAME, dry_run=True))

    assert str(refused.value) == (
        f"hive create refuses {NAME}: it is already held by a hive and the {REPO} file "
        f"k8s/manifests/{NAME}/ssh-lan.yaml; "
        "pass --name with an unused hive name, or leave it out to take the next one"
    )
    assert not world.run.named("kubectl")
    assert not (world.home / "agentihooks" / create.STATE_FILE).exists()


def test_an_explicit_name_held_only_by_an_antoncore_overlay_is_refused(world):
    world.run.tree = ["k8s/manifests/swarm-hive-007/ssh-lan.yaml"]

    with pytest.raises(auth.HiveError, match=r"^hive create refuses swarm-hive-007: it is already held by the "):
        create.create(world.tools, create.Options(name="swarm-hive-007", dry_run=True))


def test_an_explicit_name_held_only_by_a_hive_is_refused(world):
    auth.mint(world.tools.redis, "swarm-hive-007", "redis://hive.example:6380/2")

    with pytest.raises(auth.HiveError, match=r"^hive create refuses swarm-hive-007: it is already held by a hive; "):
        create.create(world.tools, create.Options(name="swarm-hive-007", dry_run=True))


def test_an_unused_explicit_name_is_created(world):
    _live_hive_one(world)

    done = create.create(world.tools, create.Options(name="swarm-hive-009", dry_run=True))

    assert done == "dry run of hive create swarm-hive-009: nothing written"


def test_a_name_only_an_antoncore_overlay_holds_is_skipped(world):
    world.run.tree = [f"k8s/manifests/{NAME}/ssh-lan.yaml"]

    done = create.create(world.tools, create.Options(dry_run=True))

    assert done == "dry run of hive create swarm-hive-002: nothing written"


def test_a_name_only_the_machine_map_holds_is_skipped(world):
    world.tools.redis.hset(machine.MACHINES, "worker-009", NAME)

    done = create.create(world.tools, create.Options(dry_run=True))

    assert done == "dry run of hive create swarm-hive-002: nothing written"


def test_a_name_with_an_open_pull_request_is_skipped_and_refused_when_given(world):
    world.run.open_heads = ["engineer-1", NAME]

    assert create.create(world.tools, create.Options(dry_run=True)) == (
        "dry run of hive create swarm-hive-002: nothing written"
    )
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(name=NAME, dry_run=True))
    assert str(refused.value).startswith(
        f"hive create refuses {NAME}: it is already held by an open {REPO} pull request; "
    )


@pytest.mark.parametrize("given", ["Swarm-Hive-009", "swarm-hive-009/", " swarm-hive-009", "swarm-hive-009-", ""])
def test_an_explicit_name_that_is_not_a_hive_name_is_refused_before_anything_is_read(world, given):
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(name=given, dry_run=True))

    assert str(refused.value) == (
        f"hive create refuses the name {given!r}: a hive name is lowercase letters, digits and dashes"
    )
    assert world.run.calls == []


@pytest.mark.parametrize("given", ["swarm-hive-010", "h", "a1-b2"])
def test_an_explicit_name_of_hive_shape_is_accepted(world, given):
    assert create.create(world.tools, create.Options(name=given, dry_run=True)) == (
        f"dry run of hive create {given}: nothing written"
    )


def test_an_unfinished_create_refuses_another_explicit_name(world):
    _keep_member(world)

    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(name="swarm-hive-009", dry_run=True))

    assert str(refused.value) == (
        f"an unfinished hive create of {NAME} is saved; rerun hive create without --name to finish it"
    )


def test_an_unfinished_create_resumes_under_its_own_explicit_name(world):
    _keep_member(world)

    create.create(world.tools, create.Options(name=NAME, dry_run=True))

    assert world.lines[0] == f"hive create {NAME}: resuming the unfinished create"


def test_cli_create_passes_its_name(monkeypatch, capsys):
    seen = {}
    monkeypatch.setenv("OPENBAO_URL", "https://bao.test")
    monkeypatch.setenv("OPENBAO_TOKEN", "token")
    monkeypatch.setattr(cli, "redis_client", lambda: None)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    monkeypatch.setattr(create, "create", lambda tools, options: seen.update(options=options) or "created")
    assert cli.main(["create", "--name", "swarm-hive-009"]) == 0
    assert seen["options"] == create.Options(name="swarm-hive-009")


def test_cli_create_names_its_name_option():
    parser = cli._parser()
    (commands,) = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    (option,) = [action for action in commands.choices["create"]._actions if action.dest == "name"]
    assert option.option_strings == ["--name"]
    assert option.help == "hive name to create; defaults to the next unused swarm-hive name"


def test_no_unused_hive_name_below_the_limit_is_a_refusal(world, monkeypatch):
    monkeypatch.setattr(create, "NAME_LIMIT", 3)
    world.run.tree = [f"k8s/manifests/{NAME}/ssh-lan.yaml"]
    world.run.open_heads = ["swarm-hive-002"]

    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(dry_run=True))

    assert str(refused.value) == "hive create found no unused hive name below swarm-hive-003"
    assert not world.run.named("kubectl")


def test_the_last_name_below_the_limit_is_taken(world, monkeypatch):
    monkeypatch.setattr(create, "NAME_LIMIT", 3)
    world.run.tree = [f"k8s/manifests/{NAME}/ssh-lan.yaml"]

    assert create.create(world.tools, create.Options(dry_run=True)) == (
        "dry run of hive create swarm-hive-002: nothing written"
    )
