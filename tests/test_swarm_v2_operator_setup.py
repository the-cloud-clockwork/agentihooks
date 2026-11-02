import argparse
import base64
import hashlib
import io
import json
import stat
import subprocess
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from scripts import cli_delegates, openbao_kv
from scripts.events.redis_state import RedisStateStore
from scripts.gates.base import Who
from scripts.swarm.store import SwarmError
from scripts.swarm_v2.deployed import pod_policy
from scripts.swarm_v2.herdr import operator_setup
from scripts.swarm_v2.herdr.attach import ClientHome
from scripts.swarm_v2.herdr.operator_setup import SetupRefused
from scripts.swarm_v2.kubernetes import operator_terminal

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
BAO_URL = "https://bao.test"
POLICY = {**json.loads((FIXTURES / "pod-policy.json").read_text()), "provider_accounts": []}
MISSING_BAO = (
    "it needs OPENBAO_URL and OPENBAO_TOKEN, set in the agentihooks env files: ~/.agentihooks/.env or a *.env "
    "companion beside it, such as ~/.agentihooks/openbao.env"
)


def ssh_private(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()


def fingerprint(key: Ed25519PrivateKey) -> str:
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    blob = base64.b64decode(public.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


class Bao:
    def __init__(self, data=None, status=0) -> None:
        self.data, self.status, self.requests = data, status, []

    def __call__(self, request, timeout=None):
        self.requests.append((request.full_url, request.get_header("X-vault-token")))
        if self.status:
            raise urllib.error.HTTPError(request.full_url, self.status, "error", {}, io.BytesIO(b'{"errors": []}'))
        body = json.dumps({"data": {"data": self.data, "metadata": {"version": 3}}}).encode()
        return io.BytesIO(body)

    def client(self) -> openbao_kv.OpenBao:
        return openbao_kv.OpenBao(BAO_URL, "fixture-token", opener=self)


def antoncore(root: Path, policy_text: str | None = None, data_key: str = "pod-policy.json") -> Path:
    text = json.dumps(POLICY) if policy_text is None else policy_text
    manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "swarm-pod-policy"}}
    manifest["data"] = {data_key: text}
    path = root / "antoncore" / "k8s" / "manifests" / "swarm" / "policy" / "pod-policy.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(manifest))
    return root / "antoncore"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_place_writes_both_files_privately_and_reports_no_value(tmp_path):
    key = Ed25519PrivateKey.generate()
    secret = ssh_private(key)
    bao = Bao({"user_ca": secret, "host_ca": "never read"})
    home = tmp_path / "home"

    report = operator_setup.place(home, bao.client(), antoncore(tmp_path))

    folder = home / "swarm-terminal"
    assert report == {
        "ca_key": str(folder / "user_ca"),
        "ca_fingerprint": fingerprint(key),
        "pod_policy": str(folder / "pod-policy.json"),
        "namespace": "swarm-pod-proof",
        "env_file": str(folder / "terminal.env"),
    }
    assert (folder / "user_ca").read_text() == secret
    assert json.loads((folder / "pod-policy.json").read_text()) == POLICY
    assert (folder / "terminal.env").read_text() == (
        f"AGENTIHOOKS_TERMINAL_CA_KEY={folder / 'user_ca'}\nAGENTIHOOKS_POD_POLICY_FILE={folder / 'pod-policy.json'}\n"
    )
    assert [mode(folder / name) for name in ("user_ca", "pod-policy.json", "terminal.env")] == [0o600] * 3
    assert mode(folder) == 0o700
    assert sorted(path.name for path in folder.iterdir()) == ["pod-policy.json", "terminal.env", "user_ca"]
    assert bao.requests == [(f"{BAO_URL}/v1/secret/data/agentihooks-swarm/terminal-attach", "fixture-token")]
    assert secret not in json.dumps(report)


def test_place_replaces_an_earlier_key_in_place(tmp_path):
    home, source = tmp_path / "home", antoncore(tmp_path)
    operator_setup.place(home, Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())}).client(), source)
    rotated = Ed25519PrivateKey.generate()

    secret = ssh_private(rotated)

    report = operator_setup.place(home, Bao({"user_ca": secret}).client(), source)

    assert report["ca_fingerprint"] == fingerprint(rotated)
    assert (home / "swarm-terminal" / "user_ca").read_text() == secret


@pytest.mark.parametrize(
    "data, status, message",
    [
        (
            {},
            0,
            "the terminal user CA is missing from OpenBao secret/agentihooks-swarm/terminal-attach field user_ca",
        ),
        (
            None,
            503,
            "the terminal user CA could not be read from OpenBao secret/agentihooks-swarm/terminal-attach: "
            "OpenBao HTTP 503: error",
        ),
        ({"user_ca": "not a key"}, 0, "the terminal user CA key is not an OpenSSH private key"),
        (
            {"user_ca": ssh_private(generate_private_key(public_exponent=65537, key_size=2048))},
            0,
            "the terminal user CA key must be Ed25519",
        ),
    ],
)
def test_a_bad_ca_record_places_nothing_and_names_no_value(tmp_path, data, status, message):
    home = tmp_path / "home"

    with pytest.raises(SetupRefused) as refused:
        operator_setup.place(home, Bao(data, status).client(), antoncore(tmp_path))

    assert str(refused.value) == message
    folder = home / "swarm-terminal"
    assert not folder.exists() or list(folder.iterdir()) == []


def test_a_bad_ca_record_keeps_the_previous_key(tmp_path):
    home, source = tmp_path / "home", antoncore(tmp_path)
    kept = ssh_private(Ed25519PrivateKey.generate())
    operator_setup.place(home, Bao({"user_ca": kept}).client(), source)

    with pytest.raises(SetupRefused):
        operator_setup.place(home, Bao({"user_ca": "not a key"}).client(), source)

    folder = home / "swarm-terminal"
    assert (folder / "user_ca").read_text() == kept
    assert sorted(path.name for path in folder.iterdir()) == ["pod-policy.json", "terminal.env", "user_ca"]


def test_an_unreadable_policy_manifest_is_refused(tmp_path):
    source = tmp_path / "antoncore"
    manifest = source / "k8s" / "manifests" / "swarm" / "policy" / "pod-policy.yaml"

    with pytest.raises(SetupRefused) as refused:
        operator_setup.place(
            tmp_path / "home", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())}).client(), source
        )

    assert str(refused.value) == f"the Pod policy manifest {manifest} is unreadable: No such file or directory"


@pytest.mark.parametrize(
    "text, data_key, message",
    [
        (None, "other.json", "carries no ConfigMap data pod-policy.json"),
        (
            "[1]",
            "pod-policy.json",
            "is refused: pod policy is invalid at the document root: [1] is not of type 'object'",
        ),
    ],
)
def test_a_bad_policy_places_no_policy_file(tmp_path, text, data_key, message):
    source = antoncore(tmp_path, text, data_key)
    manifest = source / "k8s" / "manifests" / "swarm" / "policy" / "pod-policy.yaml"
    home = tmp_path / "home"

    with pytest.raises(SetupRefused) as refused:
        operator_setup.place(home, Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())}).client(), source)

    assert str(refused.value) == f"the Pod policy in {manifest} {message}"
    assert not (home / "swarm-terminal" / "pod-policy.json").exists()
    assert not (home / "swarm-terminal" / "terminal.env").exists()
    assert [path.name for path in (home / "swarm-terminal").iterdir()] == ["user_ca"]


def test_a_manifest_that_is_not_yaml_is_refused(tmp_path):
    source = antoncore(tmp_path)
    manifest = source / "k8s" / "manifests" / "swarm" / "policy" / "pod-policy.yaml"
    manifest.write_text("data: [unclosed\n")

    with pytest.raises(SetupRefused) as refused:
        operator_setup.place(
            tmp_path / "home", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())}).client(), source
        )

    assert str(refused.value) == f"the Pod policy in {manifest} is not a YAML ConfigMap"


@pytest.mark.parametrize(
    "who, named",
    [
        (Who(name="engineer@abc-1"), "engineer@abc-1"),
        (Who(swarm="rig"), "rig"),
        (Who(name="master@abc-2", swarm="rig"), "master@abc-2"),
    ],
)
def test_setup_refuses_inside_an_agent_session(who, named):
    with pytest.raises(SetupRefused) as refused:
        operator_setup.outside_agent(who)

    assert str(refused.value) == f"the operator terminal setup runs only outside agent sessions, and {named} is one"


def test_setup_runs_outside_agent_sessions():
    assert operator_setup.outside_agent(Who()) is None


def run_main(argv, environ, who, home):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = operator_setup.main(argv, environ, who, home)
    return code, out.getvalue(), err.getvalue()


def test_setup_command_places_files_on_a_test_home_and_prints_no_value(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    secret = ssh_private(key)
    bao = Bao({"user_ca": secret})
    monkeypatch.setattr(openbao_kv.urllib.request, "urlopen", bao)
    environ = {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": "fixture-token",
        "AGENTIHOOKS_ANTONCORE_DIR": str(antoncore(tmp_path)),
    }
    home = tmp_path / "home"

    code, out, err = run_main(["setup"], environ, Who(), home)

    assert (code, err) == (0, "")
    report = json.loads(out)
    assert report["ca_fingerprint"] == fingerprint(key)
    assert Path(report["ca_key"]).read_text() == secret
    assert secret not in out
    assert "fixture-token" not in out


def test_setup_command_reads_antoncore_from_its_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        openbao_kv.urllib.request, "urlopen", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())})
    )
    environ = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": "fixture-token", "AGENTIHOOKS_ANTONCORE_DIR": "/nowhere"}

    code, out, _ = run_main(["setup", "--antoncore", str(antoncore(tmp_path))], environ, Who(), tmp_path / "home")

    assert code == 0
    assert json.loads(out)["namespace"] == "swarm-pod-proof"


def test_setup_command_defaults_antoncore_to_the_workbench_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    assert operator_setup.antoncore_dir({}, None) == tmp_path / "dev" / "tcc-ecosystem" / "antoncore"
    assert operator_setup.antoncore_dir({"AGENTIHOOKS_ANTONCORE_DIR": "/env"}, None) == Path("/env")
    assert operator_setup.antoncore_dir({"AGENTIHOOKS_ANTONCORE_DIR": "/env"}, "/flag") == Path("/flag")


@pytest.mark.parametrize(
    "environ, who, message",
    [
        ({}, Who(), f"operator terminal setup refused: {MISSING_BAO}\n"),
        (
            {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": "t"},
            Who(name="engineer@abc-1"),
            "operator terminal setup refused: the operator terminal setup runs only outside agent sessions, "
            "and engineer@abc-1 is one\n",
        ),
    ],
)
def test_setup_command_refusals_exit_one(tmp_path, environ, who, message):
    code, out, err = run_main(["setup"], environ, who, tmp_path / "home")

    assert (code, out, err) == (1, "", message)
    assert not (tmp_path / "home").exists()


def scheduler():
    import fakeredis

    from scripts.scheduler import Scheduler

    return Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")


LEGACY = (
    "agentihooks-swarm-terminal.timer",
    "agentihooks-swarm-terminal.service",
    "agentihooks-swarm-terminal-sync.service",
)


def payload(root: Path, address: str) -> str:
    return json.dumps({"address": address, "home": str(root)})


def recorded(root: Path) -> dict:
    return json.loads((root / "swarm-terminal" / "schedule.json").read_text())


class Systemctl:
    def __init__(self, code=0, error=None, stderr="") -> None:
        self.code, self.error, self.stderr, self.calls = code, error, stderr, []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.code, "", self.stderr)


QUIET = {"capture_output": True, "text": True, "timeout": 30}


def test_install_removes_the_old_systemd_units(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    for name in LEGACY:
        (units / name).write_text("old\n")
    (units / "other.timer").write_text("kept\n")
    systemctl = Systemctl()

    operator_setup.install(tmp_path / "home", "", units, systemctl, scheduler(), lambda: None)

    assert sorted(path.name for path in units.iterdir()) == ["other.timer"]
    assert systemctl.calls == [
        (
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                "agentihooks-swarm-terminal.timer",
                "agentihooks-swarm-terminal-sync.service",
            ],
            QUIET,
        ),
        (["systemctl", "--user", "daemon-reload"], {**QUIET, "check": True}),
    ]
    operator_setup.install(tmp_path / "home", "", units, systemctl, scheduler(), lambda: None)
    assert len(systemctl.calls) == 2


def test_install_names_a_refused_disable_and_keeps_the_old_units(tmp_path):
    for name in LEGACY:
        (tmp_path / name).write_text("old\n")
    machine = scheduler()

    with pytest.raises(SetupRefused) as refused:
        operator_setup.install(
            tmp_path / "home", "", tmp_path, Systemctl(code=1, stderr="Unit not loaded.\n"), machine, lambda: None
        )

    assert str(refused.value) == (
        "systemctl could not disable agentihooks-swarm-terminal.timer, "
        "agentihooks-swarm-terminal-sync.service: Unit not loaded."
    )
    assert all((tmp_path / name).exists() for name in LEGACY)
    assert operator_setup.scheduled(tmp_path / "home", "", machine)


def test_install_defaults_to_the_user_units_the_machine_scheduler_and_its_supervisor(tmp_path, monkeypatch):
    import fakeredis

    redis, started = fakeredis.FakeRedis(decode_responses=True), []
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(operator_setup, "redis_client", lambda: redis)
    monkeypatch.setattr("scripts.swarm.store.redis_client", operator_setup.redis_client)
    monkeypatch.setattr(operator_setup, "ensure_running", lambda: started.append(True))
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "agentihooks-swarm-terminal.service").write_text("old\n")
    systemctl = Systemctl()

    operator_setup.install(tmp_path / "home", run=systemctl)

    assert list(units.iterdir()) == []
    assert systemctl.calls == [(["systemctl", "--user", "daemon-reload"], {**QUIET, "check": True})]
    assert started == [True]
    machine = operator_setup.Scheduler(RedisStateStore(redis, "scheduler"))
    record = recorded(tmp_path / "home")
    assert sorted(machine.document()["entries"]) == sorted([record["refresh"], record["sync"]])
    assert operator_setup.installed(tmp_path / "home")
    (units / "agentihooks-swarm-terminal.timer").write_text("old\n")
    assert not operator_setup.installed(tmp_path / "home")


def test_parser_names_the_command_and_its_subcommands():
    parser = operator_setup._parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    setup = sub.choices["setup"]
    antoncore = next(action for action in setup._actions if action.dest == "antoncore")

    assert (parser.prog, parser.description) == ("agentihooks swarm-terminal", "Operator terminal attach setup")
    assert (sub.dest, sub.required) == ("command", True)
    assert [(choice.dest, choice.help) for choice in sub._choices_actions] == [
        ("setup", "Place the terminal user CA key and the Pod policy from their sources"),
        ("install", "Schedule setup and the herdr sync on the agentihooks scheduler, removing the old systemd units"),
        ("sync", "Run one sync of every hive's published workers into the operator's herdr"),
    ]
    assert (antoncore.option_strings, antoncore.help) == (
        ["--antoncore"],
        "antoncore checkout; defaults to AGENTIHOOKS_ANTONCORE_DIR",
    )
    with pytest.raises(SystemExit), redirect_stderr(io.StringIO()):
        parser.parse_args([])


def test_install_options_name_their_address_and_check():
    sub = next(a for a in operator_setup._parser()._actions if isinstance(a, argparse._SubParsersAction))
    options = {action.dest: action for action in sub.choices["install"]._actions}

    address, check = options["redis_address"], options["check"]
    assert (address.option_strings, address.default, address.help) == (
        ["--redis-address"],
        "",
        "hive Redis host:port setup composes, as AGENTIHOOKS_HIVE_REDIS_ADDRESS",
    )
    assert (check.option_strings, check.default, check.const, check.help) == (
        ["--check"],
        False,
        True,
        "exit 0 only when both jobs are scheduled and no old systemd unit remains",
    )


def test_setup_command_without_an_address_places_none(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(operator_setup, "place", lambda root, bao, antoncore, address: seen.append(address) or {})
    environ = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": "fixture-token"}

    assert run_main(["setup"], environ, Who(), tmp_path / "home")[0] == 0

    assert seen == [""]


@pytest.mark.parametrize("from_env", [True, False])
def test_setup_command_defaults_its_home(tmp_path, monkeypatch, from_env):
    monkeypatch.setattr(
        openbao_kv.urllib.request, "urlopen", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())})
    )
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path / "user")
    environ = {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": "fixture-token",
        "AGENTIHOOKS_ANTONCORE_DIR": str(antoncore(tmp_path)),
    }
    if from_env:
        environ["AGENTIHOOKS_HOME"] = str(tmp_path / "custom")
    root = tmp_path / "custom" if from_env else tmp_path / "user" / ".agentihooks"

    code, out, _ = run_main(["setup"], environ, Who(), None)

    assert code == 0
    assert json.loads(out)["ca_key"] == str(root / "swarm-terminal" / "user_ca")


def test_settings_keep_an_equals_sign_in_a_path(tmp_path):
    home = tmp_path / "a=b" / "home"
    operator_setup.place(
        home, Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())}).client(), antoncore(tmp_path)
    )

    assert operator_setup.settings(home)["AGENTIHOOKS_TERMINAL_CA_KEY"] == str(home / "swarm-terminal" / "user_ca")


@pytest.mark.parametrize(
    "error, named",
    [
        (subprocess.TimeoutExpired(["systemctl"], 30), "TimeoutExpired"),
        (subprocess.CalledProcessError(1, ["systemctl"]), "CalledProcessError"),
        (FileNotFoundError("systemctl"), "FileNotFoundError"),
    ],
)
def test_install_refuses_when_systemctl_fails(tmp_path, error, named):
    for name in ("agentihooks-swarm-terminal.timer", "agentihooks-swarm-terminal.service"):
        (tmp_path / name).write_text("old\n")
    with pytest.raises(SetupRefused) as refused:
        operator_setup.install(tmp_path / "home", "", tmp_path, Systemctl(error=error), scheduler(), lambda: None)

    units = "agentihooks-swarm-terminal.timer, agentihooks-swarm-terminal.service"
    assert str(refused.value) == f"systemctl could not remove {units}: {named}"


def test_install_command_names_an_unreachable_scheduler_redis_and_keeps_the_old_units(tmp_path, monkeypatch):
    import redis

    def refuse():
        raise redis.ConnectionError("Connection refused")

    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(operator_setup, "redis_client", refuse)
    monkeypatch.setattr("scripts.swarm.store.redis_client", operator_setup.redis_client)
    monkeypatch.setattr(operator_setup, "ensure_running", lambda: pytest.fail("started without a scheduler"))
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)

    assert run_main(["install", "--check"], {}, Who(), tmp_path / "home") == (
        1,
        "",
        "operator terminal install refused: the scheduler store is unreachable: ConnectionError\n",
    )
    for name in LEGACY:
        (units / name).write_text("old\n")
    assert run_main(["install"], {}, Who(), tmp_path / "home") == (
        1,
        "",
        "operator terminal install refused: the scheduler store is unreachable: ConnectionError\n",
    )
    assert sorted(path.name for path in units.iterdir()) == sorted(LEGACY)


@pytest.mark.parametrize("who", [Who(name="engineer@abc-1"), Who(swarm="rig"), Who(name="master@abc-2", swarm="rig")])
def test_install_command_schedules_inside_an_agent_launch(tmp_path, monkeypatch, who):
    calls = []
    monkeypatch.setattr(operator_setup, "install", lambda root, address: calls.append((root, address)))

    code, out, err = run_main(["install", "--redis-address", "hive:6379"], {}, who, tmp_path / "home")

    assert (code, out, err) == (0, "", "")
    assert calls == [(tmp_path / "home", "hive:6379")]


def test_install_check_reads_the_schedule_inside_an_agent_launch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(operator_setup, "installed", lambda root, address: calls.append((root, address)) or True)

    code, out, err = run_main(["install", "--check"], {}, Who(name="engineer@abc-1"), tmp_path / "home")

    assert (code, out, err) == (0, "", "")
    assert calls == [(tmp_path / "home", "")]


def test_sync_command_refuses_inside_an_agent_session(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "sync_once", lambda environ, root: pytest.fail("synced in an agent session"))

    code, out, err = run_main(["sync"], {}, Who(name="engineer@abc-1"), tmp_path / "home")

    assert (code, out) == (1, "")
    assert err == (
        "operator terminal sync refused: the operator terminal setup runs only outside agent sessions, "
        "and engineer@abc-1 is one\n"
    )


def test_install_command_exits_zero_when_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "install", lambda root, address: None)

    assert run_main(["install"], {}, Who(), tmp_path / "home") == (0, "", "")


def test_install_command_names_a_refused_enable(tmp_path, monkeypatch):
    def refuse(root, address):
        raise SetupRefused("systemctl could not disable agentihooks-swarm-terminal-sync.service: bus down")

    monkeypatch.setattr(operator_setup, "install", refuse)

    assert run_main(["install"], {}, Who(), tmp_path / "home") == (
        1,
        "",
        "operator terminal install refused: systemctl could not disable "
        "agentihooks-swarm-terminal-sync.service: bus down\n",
    )


def test_placed_settings_open_the_terminal_command_inputs(tmp_path):
    key = Ed25519PrivateKey.generate()
    home = tmp_path / "home"
    operator_setup.place(home, Bao({"user_ca": ssh_private(key)}).client(), antoncore(tmp_path))

    placed = operator_setup.settings(home)

    folder = home / "swarm-terminal"
    assert placed == {
        "AGENTIHOOKS_TERMINAL_CA_KEY": str(folder / "user_ca"),
        "AGENTIHOOKS_POD_POLICY_FILE": str(folder / "pod-policy.json"),
    }
    assert fingerprint(key) == operator_setup._fingerprint(operator_terminal.authority(placed).user_ca)
    assert pod_policy(placed["AGENTIHOOKS_POD_POLICY_FILE"])["namespace"] == "swarm-pod-proof"


def test_a_manifest_that_is_not_utf8_is_refused(tmp_path):
    source = antoncore(tmp_path)
    manifest = source / "k8s" / "manifests" / "swarm" / "policy" / "pod-policy.yaml"
    manifest.write_bytes(b"\xff\xfe data")

    with pytest.raises(SetupRefused) as refused:
        operator_setup.place(
            tmp_path / "home", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())}).client(), source
        )

    assert str(refused.value) == f"the Pod policy in {manifest} is not a YAML ConfigMap"


def test_swarm_terminal_is_a_delegated_command():
    assert cli_delegates.DELEGATED_CLIS["swarm-terminal"] == "scripts.swarm_v2.herdr.operator_setup"
    assert cli_delegates.delegated_cli(["swarm-terminal", "setup"]) is operator_setup.main


JUMP_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJumpHostKey"
JUMP_REFUSED = "AGENTIHOOKS_TERMINAL_JUMP must be host:port with AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY naming its host key"


def test_the_jump_comes_from_the_environment():
    from scripts.swarm_v2.herdr.attach import Jump

    environ = {"AGENTIHOOKS_TERMINAL_JUMP": "swarm.example:2223", "AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY": JUMP_KEY}
    assert operator_terminal.jump(environ) == Jump("swarm.example", 2223, JUMP_KEY)
    assert operator_terminal.jump({}) is None


@pytest.mark.parametrize(
    "value, key",
    [
        ("swarm.example", JUMP_KEY),
        (":2223", JUMP_KEY),
        ("swarm.example:ssh", JUMP_KEY),
        ("swarm.example:2223", ""),
        ("swarm.example:0", JUMP_KEY),
        ("swarm.example:65536", JUMP_KEY),
        ("swarm.example\n  ProxyCommand x:2223", JUMP_KEY),
    ],
)
def test_a_malformed_jump_is_refused(value, key):
    environ = {"AGENTIHOOKS_TERMINAL_JUMP": value, "AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY": key}
    with pytest.raises(SwarmError) as refused:
        operator_terminal.jump(environ)
    assert str(refused.value) == JUMP_REFUSED


@pytest.mark.parametrize(
    "value, host, port", [("swarm.example:1", "swarm.example", 1), ("fd00::7:65535", "fd00::7", 65535)]
)
def test_the_jump_takes_the_port_after_the_last_colon(value, host, port):
    from scripts.swarm_v2.herdr.attach import Jump

    environ = {"AGENTIHOOKS_TERMINAL_JUMP": value, "AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY": JUMP_KEY}
    assert operator_terminal.jump(environ) == Jump(host, port, JUMP_KEY)


def test_the_workstation_client_home_carries_the_jump(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr.attach import Jump

    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    environ = {"AGENTIHOOKS_TERMINAL_JUMP": "swarm.example:2223", "AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY": JUMP_KEY}
    assert operator_terminal.client_home(environ) == ClientHome(tmp_path, jump=Jump("swarm.example", 2223, JUMP_KEY))
    assert operator_terminal.client_home({}) == ClientHome(tmp_path)


def test_the_service_refuses_terminal_settings_it_cannot_open(tmp_path):
    root = tmp_path / "home"
    folder = root / "swarm-terminal"
    folder.mkdir(parents=True)
    (folder / "terminal.env").write_text(f"AGENTIHOOKS_TERMINAL_CA_KEY={folder / 'user_ca'}\n")
    with pytest.raises(SetupRefused) as refused:
        operator_setup.service({}, ClientHome(tmp_path / "client"), root)
    assert str(refused.value) == "opening a worker terminal needs AGENTIHOOKS_POD_POLICY_FILE"


def event(root: Path, address: str = "") -> dict:
    return {"id": "entry", "owner": "swarm-terminal", "payload": payload(root, address)}


def shell_files(monkeypatch, loaded):
    asked = []
    monkeypatch.setattr(operator_setup.operator_env, "values", lambda environ: asked.append(environ) or dict(loaded))
    return asked


def test_the_sync_slot_runs_one_pass_on_the_recorded_home_with_the_operator_shell_env(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("OPENBAO_URL", "https://stale.test")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "agentihooks"))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_REDIS_ADDRESS", raising=False)
    asked = shell_files(monkeypatch, {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": "fixture-token"})
    monkeypatch.setattr(operator_setup, "sync_once", lambda environ, root: seen.append((environ, root)) or 0)

    operator_setup.sync(event(tmp_path / "home"), "delivery", None)

    assert asked == [operator_setup.os.environ]
    [(environ, root)] = seen
    assert root == tmp_path / "home"
    assert (environ["OPENBAO_URL"], environ["OPENBAO_TOKEN"]) == (BAO_URL, "fixture-token")
    assert environ["AGENTIHOOKS_HOME"] == str(tmp_path / "agentihooks")
    assert "AGENTIHOOKS_HIVE_REDIS_ADDRESS" not in environ


def test_the_sync_slot_waits_quietly_while_a_refused_setup_holds_its_alarm(tmp_path, monkeypatch):
    shell_files(monkeypatch, {})
    monkeypatch.setattr(operator_setup, "sync_once", lambda environ, root: pytest.fail("no pass before setup"))
    folder = tmp_path / "home" / "swarm-terminal"
    folder.mkdir(parents=True)
    (folder / "alarm").write_text(json.dumps({"message": "operator terminal setup refused", "raised": ["hive-a"]}))
    err = Journal()

    with redirect_stderr(err):
        operator_setup.sync(event(tmp_path / "home"), "delivery", None)

    assert err.getvalue() == ""


def test_the_sync_slot_runs_on_placed_files_even_while_a_later_setup_is_refused(tmp_path, monkeypatch):
    seen = []
    shell_files(monkeypatch, {})
    monkeypatch.setattr(operator_setup, "sync_once", lambda environ, root: seen.append(root) or 0)
    folder = tmp_path / "home" / "swarm-terminal"
    folder.mkdir(parents=True)
    (folder / "alarm").write_text("{}")
    (folder / "terminal.env").write_text("AGENTIHOOKS_TERMINAL_CA_KEY=/ca\n")

    operator_setup.sync(event(tmp_path / "home"), "delivery", None)

    assert seen == [tmp_path / "home"]


@pytest.mark.parametrize(
    "error, line",
    [
        (
            SetupRefused("the terminal files are not placed"),
            "operator terminal sync refused: the terminal files are not placed",
        ),
        (RuntimeError("bus down"), "operator terminal sync failed: RuntimeError: bus down"),
    ],
)
def test_a_failed_scheduled_pass_is_reported_and_never_raised(tmp_path, monkeypatch, error, line):
    def fail(environ, root):
        raise error

    shell_files(monkeypatch, {})
    monkeypatch.setattr(operator_setup, "sync_once", fail)
    err = Journal()

    with redirect_stderr(err):
        operator_setup.sync(event(tmp_path / "home"), "delivery", None)

    assert (err.getvalue(), err.flushed) == (line + "\n", 1)


def test_the_refresh_slot_runs_setup_with_the_recorded_address(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_ADDRESS", "own.example:6379")
    monkeypatch.setenv("OPENBAO_URL", "https://stale.test")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "agentihooks"))
    asked = shell_files(
        monkeypatch,
        {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": "fixture-token", "AGENTIHOOKS_HIVE_REDIS_ADDRESS": "file.example:1"},
    )
    monkeypatch.setattr(operator_setup, "setup", lambda root, environ: seen.append((root, environ)) or 1)

    operator_setup.refresh(event(tmp_path / "home", "swarm.example:6379"), "delivery", None)
    operator_setup.refresh(event(tmp_path / "home"), "delivery", None)

    assert asked == [operator_setup.os.environ, operator_setup.os.environ]
    assert [root for root, _ in seen] == [tmp_path / "home", tmp_path / "home"]
    assert [environ["AGENTIHOOKS_HIVE_REDIS_ADDRESS"] for _, environ in seen] == [
        "swarm.example:6379",
        "file.example:1",
    ]
    assert [(environ["OPENBAO_URL"], environ["OPENBAO_TOKEN"]) for _, environ in seen] == [
        (BAO_URL, "fixture-token"),
        (BAO_URL, "fixture-token"),
    ]
    assert {environ["AGENTIHOOKS_HOME"] for _, environ in seen} == {str(tmp_path / "agentihooks")}


@pytest.mark.parametrize("slot, command", [(operator_setup.refresh, "setup"), (operator_setup.sync, "sync")])
def test_an_unreadable_operator_shell_env_is_reported_and_never_raised(tmp_path, monkeypatch, slot, command):
    def fail(environ):
        raise OSError("no bash")

    monkeypatch.setattr(operator_setup.operator_env, "values", fail)
    err = Journal()

    with redirect_stderr(err):
        slot(event(tmp_path / "home"), "delivery", None)

    assert err.getvalue() == f"operator terminal {command} failed: OSError: no bash\n"


def test_the_operator_shell_env_is_the_env_file_set_over_the_runner_env(tmp_path, monkeypatch):
    home = tmp_path / "operator"
    state = home / ".agentihooks"
    state.mkdir(parents=True)
    (state / ".env").write_text("OPENBAO_URL=https://main.test\nKEPT=main\n")
    (state / "openbao.env").write_text("OPENBAO_TOKEN=fixture-token\n")
    (home / ".env").write_text("PERSONAL=yes\n")
    runner = {"HOME": str(home), "PATH": operator_setup.os.environ["PATH"], "OPENBAO_URL": "https://stale.test"}
    monkeypatch.setattr(operator_setup.os, "environ", runner)

    environ = operator_setup.shell_environ()

    assert {name: environ.get(name) for name in ("OPENBAO_URL", "OPENBAO_TOKEN", "KEPT", "PERSONAL")} == {
        "OPENBAO_URL": "https://main.test",
        "OPENBAO_TOKEN": "fixture-token",
        "KEPT": "main",
        "PERSONAL": "yes",
    }
    assert runner["OPENBAO_URL"] == "https://stale.test"


class Journal(io.StringIO):
    flushed = 0

    def flush(self):
        self.flushed += 1


def test_a_failed_refresh_is_reported_and_never_raised(tmp_path, monkeypatch):
    def fail(root, environ):
        raise OSError("disk full")

    shell_files(monkeypatch, {})
    monkeypatch.setattr(operator_setup, "setup", fail)
    err = Journal()

    with redirect_stderr(err):
        operator_setup.refresh(event(tmp_path / "home"), "delivery", None)

    assert (err.getvalue(), err.flushed) == ("operator terminal setup failed: OSError: disk full\n", 1)


def test_setup_flushes_each_line_it_prints(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: pytest.fail("no ledger without a transport"))
    out, err = Journal(), Journal()

    with redirect_stdout(out), redirect_stderr(err):
        assert operator_setup.setup(tmp_path / "home", {}) == 1
    assert (out.flushed, err.flushed) == (0, 1)

    monkeypatch.setattr(
        openbao_kv.urllib.request, "urlopen", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())})
    )
    environ = {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": "fixture-token",
        "AGENTIHOOKS_ANTONCORE_DIR": str(antoncore(tmp_path)),
    }
    with redirect_stdout(out), redirect_stderr(err):
        assert operator_setup.setup(tmp_path / "home", environ) == 0
    assert (out.flushed, err.flushed) == (1, 1)


def test_a_successful_setup_drops_the_saved_sync_state_and_a_refused_one_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: pytest.fail("no ledger without a transport"))
    state = tmp_path / "home" / "swarm-terminal" / "sync-state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")

    assert run_main(["setup"], {}, Who(), tmp_path / "home")[0] == 1
    assert state.exists()

    monkeypatch.setattr(
        openbao_kv.urllib.request, "urlopen", Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())})
    )
    environ = {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": "fixture-token",
        "AGENTIHOOKS_ANTONCORE_DIR": str(antoncore(tmp_path)),
    }
    assert run_main(["setup"], environ, Who(), tmp_path / "home")[0] == 0
    assert not state.exists()


def test_install_without_the_old_timer_or_sync_only_deletes_the_setup_unit(tmp_path):
    (tmp_path / "agentihooks-swarm-terminal.service").write_text("old\n")
    systemctl = Systemctl()

    operator_setup.install(tmp_path / "home", "", tmp_path, systemctl, scheduler(), lambda: None)

    assert not (tmp_path / "agentihooks-swarm-terminal.service").exists()
    assert systemctl.calls == [(["systemctl", "--user", "daemon-reload"], {**QUIET, "check": True})]


def test_sync_command_refusals_exit_one(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    environ = {"AGENTIHOOKS_TERMINAL_JUMP": "swarm.example"}
    assert run_main(["sync"], environ, Who(), tmp_path / "home") == (
        1,
        "",
        f"operator terminal sync refused: {JUMP_REFUSED}\n",
    )


def test_sync_command_runs_one_pass_of_the_service_on_the_operator_home(tmp_path, monkeypatch):
    built, passes = {}, []
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path / "operator")

    def service(environ, client, root):
        built.update(environ=environ, client=client, root=root)
        return "service"

    monkeypatch.setattr(operator_setup, "service", service)
    monkeypatch.setattr(operator_setup, "sync_pass", lambda running, root: passes.append((running, root)))

    assert run_main(["sync"], {"A": "b"}, Who(), tmp_path / "home") == (0, "", "")
    assert built == {"environ": {"A": "b"}, "client": ClientHome(tmp_path / "operator"), "root": tmp_path / "home"}
    assert passes == [("service", tmp_path / "home")]


REDIS_PASSWORD = "p w/@:fixture"


def test_place_composes_the_hive_redis_url_privately_and_reports_only_the_address(tmp_path):
    from urllib.parse import quote

    key = Ed25519PrivateKey.generate()
    bao = Bao({"user_ca": ssh_private(key), "redis_password": REDIS_PASSWORD})
    home = tmp_path / "home"

    report = operator_setup.place(home, bao.client(), antoncore(tmp_path), "swarm.example:6379")

    folder = home / "swarm-terminal"
    url = "rediss://swarm" + ":" + quote(REDIS_PASSWORD, safe="") + "@" + "swarm.example:6379/0\n"
    assert (folder / "hive-redis.url").read_text() == url
    assert mode(folder / "hive-redis.url") == 0o600
    assert operator_setup.settings(home)["AGENTIHOOKS_HIVE_REDIS_URL_FILE"] == str(folder / "hive-redis.url")
    assert report["hive_redis"] == "swarm.example:6379"
    assert REDIS_PASSWORD not in json.dumps(report)
    assert quote(REDIS_PASSWORD, safe="") not in json.dumps(report)
    assert bao.requests[-1] == (f"{BAO_URL}/v1/secret/data/k8s/agentihooks-swarm", "fixture-token")


def test_place_without_an_address_places_no_hive_redis_url(tmp_path):
    bao = Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate()), "redis_password": REDIS_PASSWORD})
    home = tmp_path / "home"

    report = operator_setup.place(home, bao.client(), antoncore(tmp_path))

    assert "hive_redis" not in report
    assert not (home / "swarm-terminal" / "hive-redis.url").exists()
    assert "AGENTIHOOKS_HIVE_REDIS_URL_FILE" not in operator_setup.settings(home)
    assert len(bao.requests) == 1


def test_place_without_an_address_keeps_the_hive_redis_url_already_placed(tmp_path):
    bao = Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate()), "redis_password": REDIS_PASSWORD})
    home, source = tmp_path / "home", antoncore(tmp_path)
    operator_setup.place(home, bao.client(), source, "swarm.example:6379")

    report = operator_setup.place(home, bao.client(), source)

    url_file = home / "swarm-terminal" / "hive-redis.url"
    assert operator_setup.settings(home)["AGENTIHOOKS_HIVE_REDIS_URL_FILE"] == str(url_file)
    assert operator_terminal.CA_KEY_ENV in operator_setup.settings(home)
    assert url_file.exists()
    assert "hive_redis" not in report


def test_a_missing_placed_hive_redis_url_is_a_refused_transport(tmp_path):
    from scripts.swarm_v2.herdr.publications import TransportRefused

    placed = {"AGENTIHOOKS_HIVE_REDIS_URL_FILE": str(tmp_path / "gone.url")}

    with pytest.raises(TransportRefused) as refused:
        operator_setup.transport({}, placed)

    assert str(refused.value) == f"the placed hive Redis URL {tmp_path / 'gone.url'} is unreadable"


def test_a_missing_redis_password_is_refused_by_key_name(tmp_path):
    bao = Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())})
    with pytest.raises(SetupRefused) as refused:
        operator_setup.place(tmp_path / "home", bao.client(), antoncore(tmp_path), "swarm.example:6379")
    assert str(refused.value) == (
        "the hive Redis password is missing from OpenBao secret/k8s/agentihooks-swarm field redis_password"
    )


def test_setup_command_reads_the_hive_redis_address_from_its_environment(tmp_path, monkeypatch):
    bao = Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate()), "redis_password": REDIS_PASSWORD})
    monkeypatch.setattr(openbao_kv.urllib.request, "urlopen", bao)
    environ = {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": "fixture-token",
        "AGENTIHOOKS_ANTONCORE_DIR": str(antoncore(tmp_path)),
        "AGENTIHOOKS_HIVE_REDIS_ADDRESS": "swarm.example:6379",
    }

    code, out, _ = run_main(["setup"], environ, Who(), tmp_path / "home")

    assert code == 0
    assert json.loads(out)["hive_redis"] == "swarm.example:6379"


class Ledger:
    def __init__(self) -> None:
        self.alerts, self.cleared = [], []

    def raise_alert(self, slug, source, text, item=None):
        assert item == operator_setup.SETUP_ITEM
        self.alerts.append((slug, text, source))

    def close_item_alerts(self, slug, item, outcome):
        self.cleared.append((slug, item, outcome))


class Flaky(Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.failed = set()

    def raise_alert(self, slug, source, text, item=None):
        super().raise_alert(slug, source, text, item)
        if slug in self.failed or slug == "hive-c":
            return
        self.failed.add(slug)
        raise SwarmError("the ledger server is gone")


def alarm_store(slugs):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug in slugs:
        made.create(SwarmConfig(slug, "agentihooks", 1, 0))
    return made


def test_a_refused_setup_raises_one_priority_on_each_synced_swarm(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import publications

    ledger, dialled = Ledger(), []
    made = alarm_store(["hive-a", "hive-b"])
    monkeypatch.setattr(publications, "connect", lambda environ: dialled.append(dict(environ)) or made)
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    environ = {"AGENTIHOOKS_HIVE_REDIS_URL": "rediss://own.example:6379/0"}
    message = f"operator terminal setup refused: {MISSING_BAO}"

    assert run_main(["setup"], environ, Who(), tmp_path / "home") == (1, "", message + "\n")
    assert run_main(["setup"], environ, Who(), tmp_path / "home")[0] == 1

    assert ledger.alerts == [("hive-a", message, "setup"), ("hive-b", message, "setup")]
    assert dialled == [environ, environ]


def test_a_new_refusal_raises_again(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import publications

    ledger = Ledger()
    monkeypatch.setattr(publications, "connect", lambda environ: alarm_store(["hive-a"]))
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    environ = {"AGENTIHOOKS_HIVE_REDIS_URL": "rediss://own.example:6379/0"}
    run_main(["setup"], environ, Who(), tmp_path / "home")

    monkeypatch.setattr(openbao_kv.urllib.request, "urlopen", Bao(status=500))
    run_main(["setup"], {**environ, "OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": "fixture-token"}, Who(), tmp_path / "home")

    assert len(ledger.alerts) == 2
    assert ledger.alerts[1][1].startswith("operator terminal setup refused: the terminal user CA could not be read")


def test_a_dropped_or_failed_notice_is_raised_again_and_a_delivered_one_is_not(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import publications

    ledger = Flaky()
    made = alarm_store(["hive-a", "hive-b", "hive-c"])
    monkeypatch.setattr(publications, "connect", lambda environ: made)
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    environ = {"AGENTIHOOKS_HIVE_REDIS_URL": "rediss://own.example:6379/0"}

    for _ in range(3):
        assert run_main(["setup"], environ, Who(), tmp_path / "home")[0] == 1

    assert [slug for slug, _, _ in ledger.alerts] == ["hive-a", "hive-b", "hive-c", "hive-a", "hive-b"]


def test_a_successful_setup_clears_the_alarm_so_a_recurring_refusal_raises_again(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import publications

    ledger = Ledger()
    monkeypatch.setattr(publications, "connect", lambda environ: alarm_store(["hive-a"]))
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    refused = {"AGENTIHOOKS_HIVE_REDIS_URL": "rediss://own.example:6379/0"}
    bao = Bao({"user_ca": ssh_private(Ed25519PrivateKey.generate())})
    monkeypatch.setattr(openbao_kv.urllib.request, "urlopen", bao)
    placed = {
        "OPENBAO_URL": BAO_URL,
        "OPENBAO_TOKEN": "fixture-token",
        "AGENTIHOOKS_ANTONCORE_DIR": str(antoncore(tmp_path)),
    }

    assert run_main(["setup"], refused, Who(), tmp_path / "home")[0] == 1
    assert run_main(["setup"], placed, Who(), tmp_path / "home")[0] == 0
    assert not (tmp_path / "home" / "swarm-terminal" / "alarm").exists()
    assert run_main(["setup"], refused, Who(), tmp_path / "home")[0] == 1

    assert [slug for slug, _, _ in ledger.alerts] == ["hive-a", "hive-a"]
    assert ledger.cleared == [("hive-a", operator_setup.SETUP_ITEM, "The operator terminal setup succeeded.")]


def test_clearing_the_alarm_skips_a_refused_close_and_still_removes_the_alarm(tmp_path, monkeypatch):
    class Refusing(Ledger):
        def close_item_alerts(self, slug, item, outcome):
            if slug == "hive-a":
                raise SwarmError("the ledger server is gone")
            super().close_item_alerts(slug, item, outcome)

    ledger = Refusing()
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    placement = operator_setup.Placement(tmp_path / operator_setup.FOLDER)
    placement.alarm.parent.mkdir(parents=True)
    placement.alarm.write_text(json.dumps({"message": "refused", "raised": ["hive-a", "hive-b"]}))

    operator_setup.clear_alarm(placement)

    assert ledger.cleared == [("hive-b", operator_setup.SETUP_ITEM, "The operator terminal setup succeeded.")]
    assert not placement.alarm.exists()


def test_clearing_an_absent_alarm_opens_no_ledger(tmp_path, monkeypatch):
    def refuse():
        raise AssertionError("no ledger without a raised alarm")

    monkeypatch.setattr(operator_setup, "LedgerClient", refuse)
    operator_setup.clear_alarm(operator_setup.Placement(tmp_path / operator_setup.FOLDER))
    placement = operator_setup.Placement(tmp_path / "other")
    placement.alarm.parent.mkdir(parents=True)
    placement.alarm.write_text(json.dumps({"message": "refused"}))
    operator_setup.clear_alarm(placement)
    assert not placement.alarm.exists()


def test_a_refused_setup_without_a_transport_only_reaches_the_journal(tmp_path, monkeypatch):
    def refuse():
        raise AssertionError("no ledger without a transport")

    monkeypatch.setattr(operator_setup, "LedgerClient", refuse)
    code, out, err = run_main(["setup"], {}, Who(), tmp_path / "home")
    assert (code, out) == (1, "")
    assert err.startswith("operator terminal setup refused: it needs ")


def test_install_schedules_both_jobs_once_and_records_them(tmp_path):
    machine, started, root = scheduler(), [], tmp_path / "home"
    address = "swarm.example:6379"

    operator_setup.install(root, address, tmp_path, Systemctl(), machine, lambda: started.append(True))

    record = recorded(root)
    assert sorted(record) == ["payload", "refresh", "sync"]
    assert record["payload"] == payload(root, address)
    jobs = {name: machine.get(record[name]).record for name in ("refresh", "sync")}
    assert {
        name: (job["kind"], job["owner"], job["payload"], job["delay"], job["state"]) for name, job in jobs.items()
    } == {
        "refresh": ("interval", "swarm-terminal", payload(root, address), 900, "active"),
        "sync": ("interval", "swarm-terminal", payload(root, address), 15, "active"),
    }
    assert {name: [(slot["signal"], slot["reference"]) for slot in job["slots"]] for name, job in jobs.items()} == {
        "refresh": [("timeout", "scripts.swarm_v2.herdr.operator_setup:refresh")],
        "sync": [("timeout", "scripts.swarm_v2.herdr.operator_setup:sync")],
    }
    assert stat.S_IMODE((root / "swarm-terminal" / "schedule.json").stat().st_mode) == 0o600
    assert started == [True]

    operator_setup.install(root, address, tmp_path, Systemctl(), machine, lambda: started.append(True))

    assert recorded(root) == record
    assert len(machine.document()["entries"]) == 2
    assert started == [True, True]


def test_a_changed_address_replaces_both_jobs_and_cancels_the_old_ones(tmp_path):
    machine, root = scheduler(), tmp_path / "home"
    operator_setup.install(root, "swarm.example:6379", tmp_path, Systemctl(), machine, lambda: None)
    old = recorded(root)

    operator_setup.install(root, "other.example:6379", tmp_path, Systemctl(), machine, lambda: None)

    new = recorded(root)
    assert new["payload"] == payload(root, "other.example:6379")
    assert [machine.get(old[name]).state for name in ("refresh", "sync")] == ["cancelled", "cancelled"]
    assert [machine.get(new[name]).state for name in ("refresh", "sync")] == ["active", "active"]
    assert not {old["refresh"], old["sync"]} & {new["refresh"], new["sync"]}


def test_a_lost_job_or_record_is_scheduled_again_and_the_orphans_cancelled(tmp_path):
    machine, root = scheduler(), tmp_path / "home"
    operator_setup.install(tmp_path / "other", "", tmp_path, Systemctl(), machine, lambda: None)
    other = recorded(tmp_path / "other")
    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)
    first = recorded(root)
    machine.get(first["sync"]).cancel()
    assert not operator_setup.installed(root, "", tmp_path, machine)

    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)
    second = recorded(root)
    assert machine.get(first["refresh"]).state == "cancelled"
    assert operator_setup.installed(root, "", tmp_path, machine)

    (root / "swarm-terminal" / "schedule.json").write_text("not json")
    assert not operator_setup.installed(root, "", tmp_path, machine)
    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)
    third = recorded(root)
    assert [machine.get(second[name]).state for name in ("refresh", "sync")] == ["cancelled", "cancelled"]
    assert [machine.get(third[name]).state for name in ("refresh", "sync")] == ["active", "active"]
    assert [machine.get(other[name]).state for name in ("refresh", "sync")] == ["active", "active"]
    assert operator_setup.installed(root, "", tmp_path, machine)


def test_scheduled_jobs_fire_their_slots_on_the_machine_scheduler(tmp_path, monkeypatch):
    import time

    machine, root, fired = scheduler(), tmp_path / "home", []
    shell_files(monkeypatch, {})
    monkeypatch.setattr(operator_setup, "sync_once", lambda environ, home: fired.append(("sync", home)) or 0)
    monkeypatch.setattr(operator_setup, "setup", lambda home, environ: fired.append(("refresh", home)) or 0)
    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)
    now = time.time()

    assert machine.run_due(now=now + 16) == 1
    assert fired == [("sync", root)]
    assert machine.run_due(now=now + 901) == 2
    assert sorted(fired) == [("refresh", root), ("sync", root), ("sync", root)]


def test_installed_holds_only_for_live_jobs_with_the_address_and_no_old_unit(tmp_path):
    machine, root, address = scheduler(), tmp_path / "home", "swarm.example:6379"
    assert not operator_setup.installed(root, address, tmp_path, machine)
    operator_setup.install(root, address, tmp_path, Systemctl(), machine, lambda: None)

    assert operator_setup.installed(root, address, tmp_path, machine)
    assert not operator_setup.installed(root, "other.example:6379", tmp_path, machine)
    assert not operator_setup.installed(tmp_path / "elsewhere", address, tmp_path, machine)
    for name in LEGACY:
        (tmp_path / name).write_text("old\n")
        assert not operator_setup.installed(root, address, tmp_path, machine)
        (tmp_path / name).unlink()


def test_a_job_mid_delivery_still_counts_as_scheduled(tmp_path):
    machine, root = scheduler(), tmp_path / "home"
    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)

    machine.update(recorded(root)["sync"], lambda entry: entry.update(state="pending"))

    assert operator_setup.installed(root, "", tmp_path, machine)


def test_jobs_the_scheduler_no_longer_holds_are_scheduled_again(tmp_path):
    machine, root = scheduler(), tmp_path / "home"
    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)
    machine.change(lambda document: document["entries"].clear())

    assert not operator_setup.installed(root, "", tmp_path, machine)
    operator_setup.install(root, "", tmp_path, Systemctl(), machine, lambda: None)
    assert operator_setup.installed(root, "", tmp_path, machine)


def test_an_unreadable_redis_password_is_refused_naming_its_openbao_path(tmp_path):
    with pytest.raises(SetupRefused) as refused:
        operator_setup.place_redis(operator_setup.Placement(tmp_path), Bao(status=500).client(), "swarm.example:6379")

    assert str(refused.value).startswith(
        "the hive Redis password could not be read from OpenBao secret/k8s/agentihooks-swarm: "
    )


def test_install_check_command_exits_by_the_installed_state_and_writes_nothing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(operator_setup, "installed", lambda root, address: seen.append((root, address)) or False)
    monkeypatch.setattr(operator_setup, "install", lambda root, address: pytest.fail("wrote"))

    code, out, err = run_main(["install", "--check", "--redis-address", "swarm.example:6379"], {}, Who(), tmp_path)
    assert (code, out, err) == (1, "", "")
    monkeypatch.setattr(operator_setup, "installed", lambda root, address: seen.append((root, address)) or True)
    assert run_main(["install", "--check"], {}, Who(), tmp_path)[0] == 0
    assert seen == [(tmp_path, "swarm.example:6379"), (tmp_path, "")]


def test_install_command_passes_its_hive_redis_address(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(operator_setup, "install", lambda root, address: seen.append((root, address)))

    assert run_main(["install", "--redis-address", "swarm.example:6379"], {}, Who(), tmp_path / "home")[0] == 0
    assert run_main(["install"], {}, Who(), tmp_path / "home")[0] == 0
    assert seen == [(tmp_path / "home", "swarm.example:6379"), (tmp_path / "home", "")]


def test_the_scheduled_setup_never_takes_openbao_settings_from_the_runner_shell(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("OPENBAO_URL", BAO_URL)
    monkeypatch.setenv("OPENBAO_TOKEN", "shell-token")
    monkeypatch.setenv("KEPT", "runner")
    shell_files(monkeypatch, {})
    monkeypatch.setattr(operator_setup, "setup", lambda root, environ: seen.append(environ) or 1)

    operator_setup.refresh(event(tmp_path / "home"), "delivery", None)

    assert [("OPENBAO_URL" in environ, "OPENBAO_TOKEN" in environ, environ["KEPT"]) for environ in seen] == [
        (False, False, "runner")
    ]


def test_a_refused_setup_before_the_hive_transport_alerts_the_swarms_of_the_machine_redis(tmp_path, monkeypatch):
    ledger, made = Ledger(), alarm_store(["hive-a", "hive-b"])
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(operator_setup, "redis_client", lambda: made.redis)
    monkeypatch.setattr("scripts.swarm.store.redis_client", operator_setup.redis_client)
    message = f"operator terminal setup refused: {MISSING_BAO}"

    assert run_main(["setup"], {}, Who(), tmp_path / "home") == (1, "", message + "\n")

    assert ledger.alerts == [("hive-a", message, "setup"), ("hive-b", message, "setup")]
    alarm = json.loads((tmp_path / "home" / "swarm-terminal" / "alarm").read_text())
    assert alarm == {"message": message, "raised": ["hive-a", "hive-b"]}


@pytest.mark.parametrize("kind", ["redis", "socket", "swarm"])
def test_an_unreachable_machine_redis_leaves_a_refusal_in_the_journal_only(tmp_path, monkeypatch, kind):
    from redis.exceptions import ConnectionError as RedisConnectionError

    errors = {"redis": RedisConnectionError("gone"), "socket": OSError("no socket"), "swarm": SwarmError("refused")}

    def unreachable():
        raise errors[kind]

    def refuse():
        raise AssertionError("no ledger without a swarm to alert")

    monkeypatch.setattr(operator_setup, "redis_client", unreachable)
    monkeypatch.setattr("scripts.swarm.store.redis_client", operator_setup.redis_client)
    monkeypatch.setattr(operator_setup, "LedgerClient", refuse)

    assert run_main(["setup"], {}, Who(), tmp_path / "home")[0] == 1
    assert not (tmp_path / "home" / "swarm-terminal" / "alarm").exists()


@pytest.mark.parametrize("kind", ["socket", "redis"])
def test_a_hive_transport_failing_at_the_socket_falls_back_to_the_machine_redis(tmp_path, monkeypatch, kind):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from scripts.swarm_v2.herdr import publications

    class Dropped:
        def slugs(self):
            raise RedisConnectionError("dropped")

    def broken(environ):
        if kind == "socket":
            raise OSError("socket closed")
        return Dropped()

    ledger, made = Ledger(), alarm_store(["hive-a"])
    monkeypatch.setattr(publications, "connect", broken)
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(operator_setup, "redis_client", lambda: made.redis)
    monkeypatch.setattr("scripts.swarm.store.redis_client", operator_setup.redis_client)

    assert run_main(["setup"], {}, Who(), tmp_path / "home")[0] == 1
    assert [slug for slug, _, _ in ledger.alerts] == ["hive-a"]


def test_a_clean_home_install_schedules_both_jobs_and_the_setup_job_places_the_files(tmp_path, monkeypatch):
    import time

    home = tmp_path / "user"
    state = home / ".agentihooks"
    state.mkdir(parents=True)
    (state / ".env").write_text(f"AGENTIHOOKS_ANTONCORE_DIR={antoncore(tmp_path)}\n")
    (state / "openbao.env").write_text(f"OPENBAO_URL={BAO_URL}\nOPENBAO_TOKEN=configured-token\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(state))
    monkeypatch.setenv("OPENBAO_URL", "https://shell.test")
    monkeypatch.setenv("OPENBAO_TOKEN", "shell-token")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_REDIS_ADDRESS", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_ANTONCORE_DIR", raising=False)
    key = Ed25519PrivateKey.generate()
    secret = ssh_private(key)
    bao = Bao({"user_ca": secret})
    monkeypatch.setattr(openbao_kv.urllib.request, "urlopen", bao)
    synced = []
    monkeypatch.setattr(operator_setup, "sync_once", lambda environ, root: synced.append(root) or 0)
    machine, units = scheduler(), tmp_path / "units"

    operator_setup.install(state, "", units, Systemctl(), machine, lambda: None)
    assert operator_setup.installed(state, "", units, machine)
    out = io.StringIO()
    with redirect_stdout(out):
        assert machine.run_due(now=time.time() + operator_setup.REFRESH_SECONDS + 1) == 2

    placement = operator_setup.Placement(state / operator_setup.FOLDER)
    assert placement.ca_key.read_text() == secret
    assert operator_setup.settings(state) == {
        operator_setup.CA_KEY_ENV: str(placement.ca_key),
        operator_setup.POLICY_ENV: str(placement.policy),
    }
    assert {(url.split("/v1/")[0], token) for url, token in bao.requests} == {(BAO_URL, "configured-token")}
    assert json.loads(out.getvalue())["ca_fingerprint"] == fingerprint(key)
    assert synced == [state]
    assert operator_setup.installed(state, "", units, machine)
