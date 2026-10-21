import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.swarm.store as swarm_store
from scripts.hive import auth, cli, create, create_github, create_render, invite, registry
from tests.hive.test_invite import CA, LIKE, SWARM, Bao, _bao, _credential

pytestmark = pytest.mark.unit

NOW = 1_800_000_000
NAME = f"swarm-hive-{NOW}"
RECORD = f"k8s/agentihooks-hive-{NAME}"
REPO = "the-cloud-clockwork/antoncore"
PR = "https://github.com/the-cloud-clockwork/antoncore/pull/2000"
TEMPLATE = "#@ file: k8s/manifests/${name}/ssh-lan.yaml\nport: ${ssh_port}\nnode: ${node}\nkey: ${record}\n"
NFS = "        - name: HIVES\n          value: agentihooks-hive\n"
SERVICES = {
    "items": [
        {
            "metadata": {"annotations": {create_render.LAN_IP_ANNOTATION: "10.10.30.207"}},
            "spec": {"ports": [{"port": 2222}, {"port": 2223}, {"port": 2224}]},
        }
    ]
}
TARGET = "ssh://worker@10.10.30.207:2225"


class Run:
    def __init__(self):
        self.calls = []
        self.state = "OPEN"
        self.prs = []
        self.on_merge = lambda: None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, self._answer(argv, kwargs.get("input")), "")

    def _answer(self, argv, stdin):
        if argv[:3] == ["kubectl", "get", "service"]:
            return json.dumps(SERVICES)
        if argv[0] == "ssh-keygen":
            path = Path(argv[argv.index("-f") + 1])
            path.write_text("PRIVATE HOST KEY\n")
            path.with_suffix(".pub").write_text("ssh-ed25519 HOSTPUBLIC swarm\n")
            return ""
        if argv[:3] == ["herdr", "machine", "list"]:
            return "[]"
        if argv[0] != "gh":
            return ""
        return self._gh(argv[1:], stdin)

    def _gh(self, argv, stdin):
        if argv[:2] == ["api", "-H"]:
            return TEMPLATE if create_render.TEMPLATE_PATH in argv[3] else NFS
        if argv[:2] == ["pr", "list"]:
            return self.prs[0] if self.prs else ""
        if argv[:2] == ["pr", "create"]:
            self.prs.append(PR)
            return PR
        if argv[:2] == ["pr", "view"]:
            return self.state
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

    def __init__(self, redis):
        self.redis = redis

    def slugs(self):
        return ["demo"]

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
    bao = Bao({LIKE: {"credential": _credential(), "swarm_url": SWARM, "ca_pem": CA}})
    environ = {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "agentihooks")}
    run, lines = Run(), []
    published = []
    monkeypatch.setattr(create.setup, "publish", lambda env, path, dry: published.append(dry) or f"settings {dry}")
    monkeypatch.setattr(swarm_store, "RedisStore", Store)
    Store.working = True

    def beat():
        member = invite._env(bao.records[RECORD]["credential"])[invite.HIVE_ID]
        redis.hset(registry.key(member), "heartbeat_at", NOW * 1000)

    run.on_merge = beat
    tools = create.Tools(
        redis,
        _bao(bao),
        environ,
        "root",
        "redis://127.0.0.1:6379/0",
        run=run,
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
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
        f"k8s/manifests/{NAME}/ssh-lan.yaml": f"port: 2225\nnode: worker-001\nkey: {RECORD}\n",
        create_render.NFS_HOMES_PATH: f"        - name: HIVES\n          value: agentihooks-hive {NAME}\n",
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
    assert state["added"] != member
    titles = [line.split(": ")[1] for line in world.lines]
    assert titles == [title for title, _ in create.STEPS]
    assert world.lines[-1] == f"hive create {NAME}: engineer: engineer@{member} in demo works on the hive"


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
    assert world.lines[len(create.STEPS)] == f"hive create {NAME}: resuming the unfinished create"
    assert f"hive create {NAME}: manifests: kept pull request {PR}" in world.lines
    assert (world.home / ".ssh" / "known_hosts").read_text().count("HOSTPUBLIC") == 1


def test_a_finished_create_makes_a_new_hive_on_the_next_run(world):
    create.create(world.tools, create.Options(timeout_s=60))
    world.tools.clock = lambda: NOW + 7
    world.run.prs = []

    with pytest.raises(auth.HiveError):
        create.create(world.tools, create.Options(timeout_s=0))
    assert _state(world)["name"] == f"swarm-hive-{NOW + 7}"


def test_the_dry_run_prints_every_step_and_file_and_writes_nothing(world):
    done = create.create(world.tools, create.Options(dry_run=True))

    assert done == f"dry run of hive create {NAME}: nothing written"
    assert world.bao.written == []
    assert not (world.home / "agentihooks" / create.STATE_FILE).exists()
    assert not (world.home / ".ssh" / "known_hosts").exists()
    assert not world.run.named("gh", "api", "-X", "POST")
    assert not world.run.named("gh", "pr", "create")
    assert not [argv for argv in world.run.calls if argv[0] in ("ssh-keygen", "herdr", "ssh")]
    assert world.published == [True]
    assert f"--- k8s/manifests/{NAME}/ssh-lan.yaml\nport: 2225\nnode: worker-001\nkey: {RECORD}\n" in world.lines
    assert f"--- {create_render.NFS_HOMES_PATH}\n        - name: HIVES\n          value: agentihooks-hive {NAME}\n" in (
        world.lines
    )
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
        create_github.merged(run, PR)


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
