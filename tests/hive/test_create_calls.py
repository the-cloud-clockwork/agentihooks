import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.swarm.store as swarm_store
from scripts import openbao_kv
from scripts.hive import auth, cli, create, create_github, create_render, invite, machine, nats_grant, registry
from scripts.hive.bundle_origin import SETTINGS_PATH
from tests.codex_fakes import http_error
from tests.hive.test_create import NAME, NOW, PR, RECORD, REPO, RESYNC, SIGNER, TARGET, TEMPLATE, Run, Store
from tests.hive.test_invite import LIKE, SWARM, URL, Bao, _bao

pytestmark = pytest.mark.unit

PENDING = '[.[] | select(.state != "CLOSED")][0].url // ""'
MESSAGE = f"Add worker hive {NAME}\n\nRendered by agentihooks hive create from {create_render.TEMPLATE_PATH}."
LOCAL = "redis://127.0.0.1:6379/0"
GH = {"capture_output": True, "text": True, "timeout": 120, "check": False}


class Strict(Run):
    def __init__(self):
        super().__init__()
        self.kwargs = []

    def __call__(self, argv, **kwargs):
        self.kwargs.append((argv[0], kwargs))
        return super().__call__(argv, **kwargs)

    def plain(self):
        return [
            [Path(arg).name if argv[0] == "ssh-keygen" else "clone" if arg.startswith("/") else arg for arg in argv]
            if argv[0] in ("ssh-keygen", "git")
            else argv
            for argv in self.calls
        ]


class OneStore(Store):
    def slugs(self):
        assert self.redis is not None
        return ["demo"]

    def config(self, slug):
        assert slug == "demo"
        return super().config(slug)

    def agents(self, slug):
        return super().agents(slug) if slug == "demo" else []


@pytest.fixture
def world(tmp_path, monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    registry.update(redis, "root", ["ui=yes"])
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "a.pub").write_text("ssh-ed25519 FIRST a\n")
    (tmp_path / ".ssh" / "b.pub").write_text("ssh-ed25519 SECOND b\n")
    (tmp_path / ".ssh" / "known_hosts").write_text("[other]:22 ssh-ed25519 OTHER\n")
    bao = Bao(
        {LIKE: {"credential": f"AGENTIHOOKS_HIVE_URL={URL}\n", "swarm_url": SWARM}, SIGNER: {"member_swarms": {}}}
    )
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / "deep" / "agentihooks"),
        nats_grant.SIGNER_ENV: SIGNER,
    }
    run, lines, published = Strict(), [], []
    monkeypatch.setattr(create.setup, "publish", lambda *args: published.append(args) or "settings written")
    monkeypatch.setattr(swarm_store, "RedisStore", OneStore)
    Store.working = Store.placing = True
    Store.swarm_state = "running"

    def beat(at=NOW * 1000):
        member = invite._env(bao.records[RECORD]["credential"])[invite.HIVE_ID]
        redis.hset(registry.key(member), "heartbeat_at", at)

    run.on_merge = beat
    sleeps, now = [], [NOW]
    tools = create.Tools(
        redis,
        _bao(bao),
        environ,
        "root",
        LOCAL,
        "LOCAL CA\n",
        run=run,
        clock=lambda: now[0],
        sleep=lambda seconds: sleeps.append(seconds) or now.__setitem__(0, now[0] + seconds),
        out=lines.append,
    )
    return SimpleNamespace(
        tools=tools, bao=bao, run=run, lines=lines, home=tmp_path, published=published, sleeps=sleeps, beat=beat
    )


def _gh_api(path):
    return ["gh", "api", path, "--jq"]


def test_a_full_create_makes_exactly_these_calls(world):
    create.create(world.tools, create.Options(timeout_s=60))

    member = invite._env(world.bao.records[RECORD]["credential"])[invite.HIVE_ID]
    assert world.run.plain() == [
        list(machine.LIST),
        ["kubectl", "get", "service", "-A", "-o", "json"],
        RESYNC,
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", NAME, "-f", "host_key"],
        ["git", "init", "-q", "clone"],
        ["git", "-C", "clone", "remote", "add", "origin", f"https://github.com/{REPO}.git"],
        ["gh", "api", "-H", create_github.RAW, f"repos/{REPO}/contents/{create_render.TEMPLATE_PATH}?ref=dev"],
        ["gh", "pr", "list", "-R", REPO, "--head", NAME, "--state", "all", "--json", "url,state", "--jq", PENDING],
        [*_gh_api(f"repos/{REPO}/git/ref/heads/dev"), ".object.sha"],
        [*_gh_api(f"repos/{REPO}/git/commits/sha"), ".tree.sha"],
        ["gh", "api", "-X", "POST", f"repos/{REPO}/git/trees", "--input", "-", "--jq", ".sha"],
        ["gh", "api", "-X", "POST", f"repos/{REPO}/git/commits", "--input", "-", "--jq", ".sha"],
        ["gh", "api", "-X", "POST", f"repos/{REPO}/git/refs", "--input", "-", "--jq", ".sha"],
        [
            "gh",
            "pr",
            "create",
            "-R",
            REPO,
            "--base",
            "dev",
            "--head",
            NAME,
            "--title",
            f"Add worker hive {NAME}",
            "--body",
            MESSAGE,
        ],
        ["gh", "pr", "view", PR, "--json", "state", "--jq", ".state"],
        ["gh", "pr", "merge", PR, "--squash", "--auto"],
        ["gh", "pr", "view", PR, "--json", "state", "--jq", ".state"],
        list(machine.LIST),
        ["herdr", "machine", "add", "--label", NAME, "--remote-session", "default", TARGET],
    ]
    timeouts = {"kubectl": 60, "ssh-keygen": 60, "git": 120, "gh": 120, "herdr": 300}
    for name, kwargs in world.run.kwargs:
        assert {key: kwargs[key] for key in ("capture_output", "text", "timeout", "check")} == {
            **GH,
            "timeout": timeouts[name],
        }
    assert world.run.posted == [
        (
            f"repos/{REPO}/git/trees",
            {
                "base_tree": "sha",
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": text}
                    for path, text in create.create_render.render(
                        TEMPLATE, create_render.Hive(NAME, RECORD, 2225, "worker-001", "10.10.30.207")
                    ).items()
                ],
            },
        ),
        (f"repos/{REPO}/git/commits", {"message": MESSAGE, "tree": "sha", "parents": ["sha"]}),
        (f"repos/{REPO}/git/refs", {"ref": f"refs/heads/{NAME}", "sha": "sha"}),
    ]
    record = world.bao.records[RECORD]
    assert record["ca_pem"] == "LOCAL CA\n"
    assert invite._env(record["credential"])[invite.REDIS_URL].endswith("@127.0.0.1:6379/0")
    assert record[create.AUTHORIZED_KEYS] == "ssh-ed25519 FIRST a\nssh-ed25519 SECOND b\n"
    assert record[create.HOST_PUBLIC_KEY] == "ssh-ed25519 HOSTPUBLIC swarm"
    assert world.published == [(world.tools.environ, SETTINGS_PATH, False)]
    assert (world.home / ".ssh" / "known_hosts").read_text() == (
        "[other]:22 ssh-ed25519 OTHER\n[10.10.30.207]:2225 ssh-ed25519 HOSTPUBLIC swarm\n"
    )
    state = world.home / "deep" / "agentihooks" / create.STATE_FILE
    assert state.read_text() == json.dumps(json.loads(state.read_text()), indent=2) + "\n"
    assert world.sleeps == []
    assert world.lines == [
        f"hive create {NAME}: invite: invited hive member {member} as {NAME}; "
        f"OpenBao secret/{RECORD}: wrote ca_pem, credential, swarm_url (3 keys total); "
        f"listed {member} on the hub signer record {SIGNER} for demo "
        "and asked the hub's agentihooks-swarm-nats-signer sync to refresh it now",
        f"hive create {NAME}: ssh keys: OpenBao secret/{RECORD}: wrote ssh_authorized_keys, ssh_host_key, "
        "ssh_host_public_key (6 keys total)",
        f"hive create {NAME}: manifests: opened pull request {PR}",
        f"hive create {NAME}: merge: merged {PR}",
        f"hive create {NAME}: settings: settings written",
        f"hive create {NAME}: live: hive {member} is live",
        f"hive create {NAME}: herdr: added {TARGET} to herdr; its hive member is {member}",
        f"hive create {NAME}: engineer: engineer@{member} in demo works on the hive",
    ]


def test_the_dry_run_names_the_pull_request_files(world):
    create.create(world.tools, create.Options(dry_run=True))
    files = f"k8s/manifests/{NAME}/ssh-lan.yaml"
    assert f"hive create {NAME}: manifests: would open a pull request into {REPO} dev with {files}" in world.lines
    keys = f"hive create {NAME}: ssh keys: would write a new SSH host key and 2 authorized keys into OpenBao secret/{RECORD}"
    assert keys in world.lines
    assert (
        f"hive create {NAME}: herdr: would trust the hive host key and save {TARGET} in herdr as {NAME}" in world.lines
    )
    assert world.published == [(world.tools.environ, SETTINGS_PATH, True)]


def test_an_already_merged_pull_request_is_not_merged_again(world, monkeypatch):
    monkeypatch.setattr(create.create_github, "pr_state", lambda run, url: (world.beat(), "MERGED")[1])
    Store.working = False
    with pytest.raises(auth.HiveError, match="timed out after 0s waiting for an engineer working on the hive"):
        create.create(world.tools, create.Options(timeout_s=0))
    assert not world.run.named("gh", "pr", "merge")


def test_a_pull_request_that_never_merges_times_out_naming_it(world):
    world.run.on_merge = lambda: setattr(world.run, "state", "OPEN")
    with pytest.raises(auth.HiveError, match=f"waiting for pull request {PR} to merge; rerun hive create to resume"):
        create.create(world.tools, create.Options(timeout_s=0))


ROLLUP = ["gh", "pr", "view", PR, "--json", "statusCheckRollup"]
STATE = ["gh", "pr", "view", PR, "--json", "state", "--jq", ".state"]
AUTO_REFUSED = "GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)"


def _merge_calls(run):
    return [argv for argv in run.calls if argv[:2] == ["gh", "pr"] and argv[2] in ("view", "merge")]


def test_a_refused_auto_merge_waits_for_checks_then_squash_merges(world):
    world.run.merge_error = AUTO_REFUSED
    world.run.checks = [
        [{"__typename": "CheckRun", "name": "Gate", "status": "QUEUED", "conclusion": ""}],
        [{"__typename": "CheckRun", "name": "Gate", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    ]
    create.create(world.tools, create.Options(timeout_s=60))

    assert _merge_calls(world.run) == [
        STATE,
        ["gh", "pr", "merge", PR, "--squash", "--auto"],
        ROLLUP,
        ROLLUP,
        ["gh", "pr", "merge", PR, "--squash"],
        STATE,
    ]
    assert world.sleeps[0] == create.POLL_S
    assert f"hive create {NAME}: merge: merged {PR}" in world.lines


def test_a_failed_check_stops_the_create_without_merging(world):
    world.run.merge_error = AUTO_REFUSED
    world.run.checks = [[{"__typename": "StatusContext", "context": "Gate", "state": "FAILURE"}]]
    with pytest.raises(auth.HiveError, match=f"pull request {PR} has failed checks: Gate;"):
        create.create(world.tools, create.Options(timeout_s=60))
    assert ["gh", "pr", "merge", PR, "--squash"] not in world.run.calls


def test_a_failed_check_the_base_does_not_require_still_merges(world):
    world.run.merge_error = AUTO_REFUSED
    world.run.required = ["Gate — Required"]
    world.run.checks = [
        [
            {"__typename": "CheckRun", "name": "Gate — Required", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "Gate — Protected", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    ]
    create.create(world.tools, create.Options(timeout_s=60))
    assert ["gh", "pr", "merge", PR, "--squash"] in world.run.calls
    assert f"hive create {NAME}: merge: merged {PR}" in world.lines


def test_checks_that_never_pass_time_out_naming_them(world):
    world.run.merge_error = AUTO_REFUSED
    world.run.checks = [[]]
    with pytest.raises(auth.HiveError, match=f"waiting for pull request {PR} checks to pass; rerun hive create"):
        create.create(world.tools, create.Options(timeout_s=0))


def test_a_merge_error_other_than_the_auto_merge_refusal_stops_the_create(world):
    world.run.merge_error = "Pull request is not mergeable"
    with pytest.raises(auth.HiveError, match="gh pr merge exited 1: Pull request is not mergeable"):
        create.create(world.tools, create.Options(timeout_s=60))
    assert ROLLUP not in world.run.calls


def test_a_stale_heartbeat_is_not_live(world):
    world.run.on_merge = lambda: world.beat(NOW * 1000 - registry.LIVE_MS - 1)
    with pytest.raises(auth.HiveError, match="waiting for hive .* to send a heartbeat"):
        create.create(world.tools, create.Options(timeout_s=0))


def test_a_wait_sleeps_between_checks_until_its_deadline(world):
    times = iter([NOW, NOW, NOW + 30, NOW + 61])
    world.tools.clock = lambda: next(times)
    checks = iter([False, False, False])
    with pytest.raises(auth.HiveError, match="timed out after 60s waiting for it"):
        create._wait(world.tools, create.Options(timeout_s=60), "it", lambda: next(checks))
    assert world.sleeps == [create.POLL_S, create.POLL_S]


def test_a_wait_with_no_time_left_checks_once_and_never_sleeps(world):
    with pytest.raises(auth.HiveError, match="timed out after 0s waiting for it"):
        create._wait(world.tools, create.Options(timeout_s=0), "it", lambda: False)
    assert world.sleeps == []


def test_a_master_is_not_the_engineer(world, monkeypatch):
    class Masters(OneStore):
        def agents(self, slug):
            return [
                SimpleNamespace(name=a.name, hive=a.hive, lane="master", state=a.state) for a in super().agents(slug)
            ]

    monkeypatch.setattr(swarm_store, "RedisStore", Masters)
    with pytest.raises(auth.HiveError, match="waiting for an engineer"):
        create.create(world.tools, create.Options(timeout_s=0))


def test_the_placement_refusal_reads_in_full(world):
    Store.placing = False
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options())
    assert str(refused.value) == (
        "no running swarm places engineers on hives, so the new hive would wait for one forever: "
        "run agentihooks swarm <slug> set hive-placement=on on a running swarm"
    )


@pytest.mark.parametrize("state", ["paused", "stopped"])
def test_a_swarm_that_is_not_running_does_not_count_as_placing(world, state):
    Store.swarm_state = state
    with pytest.raises(auth.HiveError, match="no running swarm places engineers on hives"):
        create.create(world.tools, create.Options())
    assert world.run.calls == []


def test_the_missing_key_refusal_reads_in_full(world):
    for key in (world.home / ".ssh").glob("*.pub"):
        key.unlink()
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options())
    assert str(refused.value) == "no public key under ~/.ssh to authorize on the hive sshd"


class Sealed(Bao):
    def __init__(self, records, sealed):
        super().__init__(records)
        self.sealed = sealed

    def __call__(self, request, timeout=None):
        if self.sealed(request):
            raise http_error(request.full_url, 503, b'{"errors": ["sealed"]}')
        return super().__call__(request, timeout)


@pytest.mark.parametrize(
    "sealed",
    [
        lambda request: request.get_method() == "GET" and RECORD in request.full_url,
        lambda request: request.get_method() != "GET" and create.HOST_KEY.encode() in (request.data or b""),
    ],
)
def test_an_openbao_failure_is_a_refusal_naming_it(world, sealed):
    world.tools.bao = openbao_kv.OpenBao(world.tools.bao.url, world.tools.bao.token, Sealed(world.bao.records, sealed))
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options())
    assert str(refused.value) == "OpenBao HTTP 503: sealed"


def test_a_hub_record_that_cannot_be_read_is_a_refusal_naming_it(world):
    sealed = Sealed(world.bao.records, lambda request: SIGNER in request.full_url)
    world.tools.bao = openbao_kv.OpenBao(world.tools.bao.url, world.tools.bao.token, sealed)
    with pytest.raises(auth.HiveError) as refused:
        create.create(world.tools, create.Options(dry_run=True))
    assert str(refused.value) == "OpenBao HTTP 503: sealed"


def test_gh_keeps_leading_space_and_trims_only_trailing_newlines():
    def run(argv, **kwargs):
        assert kwargs == {"input": '{"a": 1}', **GH, "cwd": None}
        return subprocess.CompletedProcess(argv, 0, "  text X\n\n", "")

    assert create_github.gh(run, ["api", "x"], {"a": 1}) == "  text X"

    def spaced(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "  text \n", "")

    assert create_github.gh(spaced, ["api", "x"]) == "  text "


def test_gh_that_cannot_run_names_its_subcommand():
    def run(argv, **kwargs):
        raise OSError("no gh")

    with pytest.raises(auth.HiveError) as refused:
        create_github.gh(run, ["pr", "view"])
    assert str(refused.value) == "gh pr failed (no gh)"


def test_a_branch_left_behind_is_forced_to_the_new_commit():
    run = Strict()
    seen = []

    def answer(argv, **kwargs):
        if argv[:5] == ["gh", "api", "-X", "POST", f"repos/{REPO}/git/refs"]:
            return subprocess.CompletedProcess(argv, 1, "", "Reference already exists")
        seen.append(kwargs.get("input"))
        return run(argv, **kwargs)

    create_github._branch(answer, REPO, NAME, "c0ffee")
    assert json.loads(seen[-1]) == {"sha": "c0ffee", "force": True}


def test_cli_create_hands_its_tools_the_live_settings(monkeypatch):
    seen = {}
    monkeypatch.setenv("OPENBAO_URL", "https://bao.test")
    monkeypatch.setenv("OPENBAO_TOKEN", "token")
    monkeypatch.setattr(cli, "redis_client", lambda: "REDIS")
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    monkeypatch.setattr("scripts.swarm.store.redis_url", lambda environ: "redis://local")
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: "SERVED CA")
    monkeypatch.setattr(create, "create", lambda tools, options: seen.update(tools=tools) or "created")
    assert cli.main(["create"]) == 0
    tools = seen["tools"]
    assert (tools.redis, tools.bao.url, tools.bao.token, tools.environ, tools.local_id) == (
        "REDIS",
        "https://bao.test",
        "token",
        os.environ,
        "root",
    )
    assert (tools.local_redis_url, tools.ledger_ca) == ("redis://local", "SERVED CA")


def test_cli_create_help_names_every_option(capsys):
    with pytest.raises(SystemExit):
        cli.main(["create", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    for line in (
        "GitOps repository of the hive; defaults to antoncore",
        "branch the hive pull request merges into; defaults to dev",
        "OpenBao record to copy; defaults to the workstation hive's",
        "node the hive pod runs on; defaults to worker-001",
        "seconds each wait may take; defaults to 1800",
        "Print every step and file, and write nothing",
    ):
        assert f" {line} " in f" {text} "
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    top = " ".join(capsys.readouterr().out.split())
    assert " Create a Kubernetes hive from nothing and add it to herdr " in f" {top} "


def test_cli_create_timeout_is_a_number(capsys):
    with pytest.raises(SystemExit):
        cli.main(["create", "--timeout", "soon"])
    assert "invalid int value" in capsys.readouterr().err


def test_a_record_read_reaches_openbao_at_the_record():
    requests = []

    def opener(request, timeout=None):
        requests.append(request.full_url)
        return io.BytesIO(json.dumps({"data": {"data": {}, "metadata": {"version": 1}}}).encode())

    tools = SimpleNamespace(bao=openbao_kv.OpenBao("https://bao.test", "t", opener), state={"record": RECORD})
    assert create._record(tools) == {}
    assert requests == [f"https://bao.test/v1/secret/data/{RECORD}"]
