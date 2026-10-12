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
from scripts.gates.base import Who
from scripts.swarm.store import SwarmError
from scripts.swarm_v2.deployed import pod_policy
from scripts.swarm_v2.herdr import operator_setup
from scripts.swarm_v2.herdr.attach import ClientHome
from scripts.swarm_v2.herdr.operator_setup import SetupRefused
from scripts.swarm_v2.kubernetes import operator_terminal

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
BAO_URL = "https://bao.test"
POLICY = {**json.loads((FIXTURES / "pod-policy.json").read_text()), "provider_accounts": []}


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
        ({}, Who(), "operator terminal setup refused: it needs OPENBAO_URL and OPENBAO_TOKEN\n"),
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


def test_service_unit_runs_setup_outside_agent_sessions():
    assert operator_setup.unit("/opt/bin/agentihooks") == (
        "[Unit]\nDescription=agentihooks operator terminal setup\n\n"
        "[Service]\nType=oneshot\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        'ExecStart="/opt/bin/agentihooks" swarm-terminal setup\n'
        "ExecStartPost=-systemctl --user try-restart agentihooks-swarm-terminal-sync.service\n"
    )
    assert operator_setup.timer() == (
        "[Unit]\nDescription=agentihooks operator terminal setup, refreshed\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=15min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )


class Systemctl:
    def __init__(self, code=0, error=None, stderr="") -> None:
        self.code, self.error, self.stderr, self.calls = code, error, stderr, []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.code, "", self.stderr)


QUIET = {"capture_output": True, "text": True, "timeout": 30}


def test_install_writes_and_enables_the_timer(tmp_path):
    systemctl = Systemctl()
    tmp_path = tmp_path / "units"

    operator_setup.install("/opt/bin/agentihooks", tmp_path, systemctl)
    assert (tmp_path / "agentihooks-swarm-terminal.service").read_text() == operator_setup.unit("/opt/bin/agentihooks")
    assert (tmp_path / "agentihooks-swarm-terminal.timer").read_text() == operator_setup.timer()
    assert (tmp_path / "agentihooks-swarm-terminal-sync.service").read_text() == operator_setup.sync_unit(
        "/opt/bin/agentihooks"
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "agentihooks-swarm-terminal-sync.service",
        "agentihooks-swarm-terminal.service",
        "agentihooks-swarm-terminal.timer",
    ]
    enable = [
        (["systemctl", "--user", "enable", "--now", "agentihooks-swarm-terminal.timer"], QUIET),
        (["systemctl", "--user", "enable", "--now", "agentihooks-swarm-terminal-sync.service"], QUIET),
    ]
    assert systemctl.calls == [(["systemctl", "--user", "daemon-reload"], {**QUIET, "check": True}), *enable]

    operator_setup.install("/opt/bin/agentihooks", tmp_path, systemctl)
    assert systemctl.calls[3:] == enable


def test_install_reports_a_failed_enable(tmp_path):
    with pytest.raises(SetupRefused) as refused:
        operator_setup.install("/opt/bin/agentihooks", tmp_path, Systemctl(code=1, stderr="Unit not found.\n"))
    assert str(refused.value) == (
        "systemctl could not enable agentihooks-swarm-terminal.timer: Unit not found.; "
        "agentihooks-swarm-terminal-sync.service: Unit not found."
    )


def test_install_defaults_to_the_agentihooks_executable_and_user_units(tmp_path, monkeypatch):
    looked_up = []
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(operator_setup.shutil, "which", lambda name: looked_up.append(name) or "/opt/bin/agentihooks")

    operator_setup.install(run=Systemctl())

    units = tmp_path / ".config" / "systemd" / "user"
    assert looked_up == ["agentihooks"]
    assert sorted(path.name for path in units.iterdir()) == [
        "agentihooks-swarm-terminal-sync.service",
        "agentihooks-swarm-terminal.service",
        "agentihooks-swarm-terminal.timer",
    ]
    assert (units / "agentihooks-swarm-terminal.service").read_text() == operator_setup.unit("/opt/bin/agentihooks")


def test_parser_names_the_command_and_its_subcommands():
    parser = operator_setup._parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    setup = sub.choices["setup"]
    antoncore = next(action for action in setup._actions if action.dest == "antoncore")

    assert (parser.prog, parser.description) == ("agentihooks swarm-terminal", "Operator terminal attach setup")
    assert (sub.dest, sub.required) == ("command", True)
    assert [(choice.dest, choice.help) for choice in sub._choices_actions] == [
        ("setup", "Place the terminal user CA key and the Pod policy from their sources"),
        ("install", "Write and enable the user timer that runs setup and the sync service"),
        ("sync", "Keep every hive's published workers in the operator's herdr"),
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
        "exit 0 only when the units are current and enabled",
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
    with pytest.raises(SetupRefused) as refused:
        operator_setup.install("/opt/bin/agentihooks", tmp_path, Systemctl(error=error))

    units = "agentihooks-swarm-terminal.timer and agentihooks-swarm-terminal-sync.service"
    assert str(refused.value) == f"systemctl could not enable {units}: {named}"


def test_install_command_refuses_without_the_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup.shutil, "which", lambda name: None)

    code, out, err = run_main(["install"], {}, Who(), tmp_path / "home")

    assert (code, out, err) == (1, "", "operator terminal install refused: the agentihooks executable is not on PATH\n")


def test_install_command_refuses_inside_an_agent_session(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "install", lambda address: pytest.fail("installed inside an agent session"))

    code, out, err = run_main(["install"], {}, Who(name="engineer@abc-1"), tmp_path / "home")

    assert (code, out) == (1, "")
    assert err == (
        "operator terminal install refused: the operator terminal setup runs only outside agent sessions, "
        "and engineer@abc-1 is one\n"
    )


def test_install_command_exits_zero_when_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "install", lambda address: None)

    assert run_main(["install"], {}, Who(), tmp_path / "home") == (0, "", "")


def test_install_command_names_a_refused_enable(tmp_path, monkeypatch):
    def refuse(address):
        raise SetupRefused("systemctl could not enable agentihooks-swarm-terminal-sync.service: bus down")

    monkeypatch.setattr(operator_setup, "install", refuse)

    assert run_main(["install"], {}, Who(), tmp_path / "home") == (
        1,
        "",
        "operator terminal install refused: systemctl could not enable "
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


def test_the_sync_unit_keeps_the_service_running():
    assert operator_setup.sync_unit("/opt/bin/agentihooks") == (
        "[Unit]\nDescription=agentihooks operator herdr sync of hive workers\n\n"
        "[Service]\nType=simple\nRestart=always\nRestartSec=30\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        'ExecStart="/opt/bin/agentihooks" swarm-terminal sync\n\n'
        "[Install]\nWantedBy=default.target\n"
    )


def test_install_reports_a_failed_sync_enable(tmp_path):
    class Refusing(Systemctl):
        def __call__(self, command, **kwargs):
            self.code = 1 if command[-1] == "agentihooks-swarm-terminal-sync.service" else 0
            return super().__call__(command, **kwargs)

    with pytest.raises(SetupRefused) as refused:
        operator_setup.install("/opt/bin/agentihooks", tmp_path, Refusing(stderr="bus down"))
    assert str(refused.value) == "systemctl could not enable agentihooks-swarm-terminal-sync.service: bus down"


def test_sync_command_refusals_exit_one(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    environ = {"AGENTIHOOKS_TERMINAL_JUMP": "swarm.example"}
    assert run_main(["sync"], environ, Who(), tmp_path / "home") == (
        1,
        "",
        f"operator terminal sync refused: {JUMP_REFUSED}\n",
    )


def test_sync_command_runs_the_service_on_the_operator_home(tmp_path, monkeypatch):
    built = {}
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path / "operator")

    def service(environ, client, root):
        built.update(environ=environ, client=client, root=root)
        return "service"

    monkeypatch.setattr(operator_setup, "service", service)
    monkeypatch.setattr(operator_setup, "serve", lambda running: 0 if running == "service" else 2)

    assert run_main(["sync"], {"A": "b"}, Who(), tmp_path / "home") == (0, "", "")
    assert built == {"environ": {"A": "b"}, "client": ClientHome(tmp_path / "operator"), "root": tmp_path / "home"}


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
        self.followups = []

    def followup(self, slug, text, needs_operator=False):
        self.followups.append((slug, text, needs_operator))
        return True


class Flaky(Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.failed = set()

    def followup(self, slug, text, needs_operator=False):
        super().followup(slug, text, needs_operator)
        if slug in self.failed:
            return True
        self.failed.add(slug)
        if slug == "hive-b":
            raise SwarmError("the ledger server is gone")
        return slug != "hive-a"


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
    message = f"operator terminal setup refused: it needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV}"

    assert run_main(["setup"], environ, Who(), tmp_path / "home") == (1, "", message + "\n")
    assert run_main(["setup"], environ, Who(), tmp_path / "home")[0] == 1

    assert ledger.followups == [("hive-a", message, True), ("hive-b", message, True)]
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

    assert len(ledger.followups) == 2
    assert ledger.followups[1][1].startswith("operator terminal setup refused: the terminal user CA could not be read")


def test_a_dropped_or_failed_notice_is_raised_again_and_a_delivered_one_is_not(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import publications

    ledger = Flaky()
    made = alarm_store(["hive-a", "hive-b", "hive-c"])
    monkeypatch.setattr(publications, "connect", lambda environ: made)
    monkeypatch.setattr(operator_setup, "LedgerClient", lambda: ledger)
    environ = {"AGENTIHOOKS_HIVE_REDIS_URL": "rediss://own.example:6379/0"}

    for _ in range(3):
        assert run_main(["setup"], environ, Who(), tmp_path / "home")[0] == 1

    assert [slug for slug, _, _ in ledger.followups] == ["hive-a", "hive-b", "hive-c", "hive-a", "hive-b"]


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

    assert [slug for slug, _, _ in ledger.followups] == ["hive-a", "hive-a"]


def test_a_refused_setup_without_a_transport_only_reaches_the_journal(tmp_path, monkeypatch):
    def refuse():
        raise AssertionError("no ledger without a transport")

    monkeypatch.setattr(operator_setup, "LedgerClient", refuse)
    code, out, err = run_main(["setup"], {}, Who(), tmp_path / "home")
    assert (code, out) == (1, "")
    assert err.startswith("operator terminal setup refused: it needs ")


def test_install_writes_the_hive_redis_address_into_the_setup_unit(tmp_path):
    operator_setup.install("/opt/bin/agentihooks", tmp_path, Systemctl(), "swarm.example:6379")

    unit = (tmp_path / "agentihooks-swarm-terminal.service").read_text()
    assert unit == operator_setup.unit("/opt/bin/agentihooks", "swarm.example:6379")
    assert "Environment=AGENTIHOOKS_HIVE_REDIS_ADDRESS=swarm.example:6379\n" in unit.splitlines(keepends=True)
    assert "AGENTIHOOKS_HIVE_REDIS_ADDRESS" not in operator_setup.unit("/opt/bin/agentihooks")


def test_installed_holds_only_for_current_enabled_units_with_the_address(tmp_path):
    address = "swarm.example:6379"
    assert not operator_setup.installed("/opt/bin/agentihooks", tmp_path, Systemctl(), address)
    operator_setup.install("/opt/bin/agentihooks", tmp_path, Systemctl(), address)

    assert operator_setup.installed("/opt/bin/agentihooks", tmp_path, Systemctl(), address)
    assert not operator_setup.installed("/opt/bin/agentihooks", tmp_path, Systemctl(), "other.example:6379")
    assert not operator_setup.installed("/opt/bin/agentihooks", tmp_path, Systemctl(code=1), address)
    (tmp_path / "agentihooks-swarm-terminal-sync.service").write_text("stale\n")
    assert not operator_setup.installed("/opt/bin/agentihooks", tmp_path, Systemctl(), address)


def test_installed_asks_systemctl_about_each_unit_on_its_own(tmp_path):
    systemctl = Systemctl()
    operator_setup.install("/opt/bin/agentihooks", tmp_path, Systemctl())

    assert operator_setup.installed("/opt/bin/agentihooks", tmp_path, systemctl)

    assert systemctl.calls == [
        (["systemctl", "--user", "is-enabled", "--quiet", "agentihooks-swarm-terminal.timer"], QUIET),
        (["systemctl", "--user", "is-enabled", "--quiet", "agentihooks-swarm-terminal-sync.service"], QUIET),
    ]


def test_installed_defaults_to_the_agentihooks_on_path_and_the_user_unit_folder(tmp_path, monkeypatch):
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    operator_setup.install("/opt/bin/agentihooks", unit_dir, Systemctl())
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(operator_setup.shutil, "which", {"agentihooks": "/opt/bin/agentihooks"}.get)

    assert operator_setup.installed(run=Systemctl())
    monkeypatch.setattr(operator_setup.shutil, "which", lambda name: None)
    assert not operator_setup.installed(run=Systemctl())


def test_an_unreadable_redis_password_is_refused_naming_its_openbao_path(tmp_path):
    with pytest.raises(SetupRefused) as refused:
        operator_setup.place_redis(operator_setup.Placement(tmp_path), Bao(status=500).client(), "swarm.example:6379")

    assert str(refused.value).startswith(
        "the hive Redis password could not be read from OpenBao secret/k8s/agentihooks-swarm: "
    )


def test_install_check_command_exits_by_the_installed_state_and_writes_nothing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(operator_setup, "installed", lambda address: seen.append(address) or False)
    monkeypatch.setattr(operator_setup, "install", lambda address: (_ for _ in ()).throw(AssertionError("wrote")))

    code, out, err = run_main(["install", "--check", "--redis-address", "swarm.example:6379"], {}, Who(), tmp_path)
    assert (code, out, err) == (1, "", "")
    monkeypatch.setattr(operator_setup, "installed", lambda address: seen.append(address) or True)
    assert run_main(["install", "--check"], {}, Who(), tmp_path)[0] == 0
    assert seen == ["swarm.example:6379", ""]


def test_install_command_passes_its_hive_redis_address(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(operator_setup, "install", lambda address: seen.append(address))

    assert run_main(["install", "--redis-address", "swarm.example:6379"], {}, Who(), tmp_path / "home")[0] == 0
    assert run_main(["install"], {}, Who(), tmp_path / "home")[0] == 0
    assert seen == ["swarm.example:6379", ""]
