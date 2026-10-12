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
from scripts.swarm_v2.deployed import pod_policy
from scripts.swarm_v2.herdr import operator_setup
from scripts.swarm_v2.herdr.operator_setup import SetupRefused
from scripts.swarm_v2.kubernetes import operator_terminal

pytestmark = pytest.mark.unit
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
    )
    assert operator_setup.timer() == (
        "[Unit]\nDescription=agentihooks operator terminal setup, refreshed\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=15min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )


class Systemctl:
    def __init__(self, code=0, error=None) -> None:
        self.code, self.error, self.calls = code, error, []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.code, "", "")


QUIET = {"capture_output": True, "text": True, "timeout": 30}


def test_install_writes_and_enables_the_timer(tmp_path):
    systemctl = Systemctl()
    tmp_path = tmp_path / "units"

    assert operator_setup.install("/opt/bin/agentihooks", tmp_path, systemctl) is True
    assert (tmp_path / "agentihooks-swarm-terminal.service").read_text() == operator_setup.unit("/opt/bin/agentihooks")
    assert (tmp_path / "agentihooks-swarm-terminal.timer").read_text() == operator_setup.timer()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "agentihooks-swarm-terminal.service",
        "agentihooks-swarm-terminal.timer",
    ]
    assert systemctl.calls == [
        (["systemctl", "--user", "daemon-reload"], {**QUIET, "check": True}),
        (["systemctl", "--user", "enable", "--now", "agentihooks-swarm-terminal.timer"], QUIET),
    ]

    assert operator_setup.install("/opt/bin/agentihooks", tmp_path, systemctl) is True
    assert systemctl.calls[2:] == [
        (["systemctl", "--user", "enable", "--now", "agentihooks-swarm-terminal.timer"], QUIET)
    ]


def test_install_reports_a_failed_enable(tmp_path):
    assert operator_setup.install("/opt/bin/agentihooks", tmp_path, Systemctl(code=1)) is False


def test_install_defaults_to_the_agentihooks_executable_and_user_units(tmp_path, monkeypatch):
    looked_up = []
    monkeypatch.setattr(operator_setup.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(operator_setup.shutil, "which", lambda name: looked_up.append(name) or "/opt/bin/agentihooks")

    assert operator_setup.install(run=Systemctl()) is True

    units = tmp_path / ".config" / "systemd" / "user"
    assert looked_up == ["agentihooks"]
    assert sorted(path.name for path in units.iterdir()) == [
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
        ("install", "Write and enable the user timer that runs setup"),
    ]
    assert (antoncore.option_strings, antoncore.help) == (
        ["--antoncore"],
        "antoncore checkout; defaults to AGENTIHOOKS_ANTONCORE_DIR",
    )
    with pytest.raises(SystemExit), redirect_stderr(io.StringIO()):
        parser.parse_args([])


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

    assert str(refused.value) == f"systemctl could not enable agentihooks-swarm-terminal.timer: {named}"


def test_install_command_refuses_without_the_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup.shutil, "which", lambda name: None)

    code, out, err = run_main(["install"], {}, Who(), tmp_path / "home")

    assert (code, out, err) == (1, "", "operator terminal install refused: the agentihooks executable is not on PATH\n")


def test_install_command_refuses_inside_an_agent_session(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_setup, "install", lambda: pytest.fail("installed inside an agent session"))

    code, out, err = run_main(["install"], {}, Who(name="engineer@abc-1"), tmp_path / "home")

    assert (code, out) == (1, "")
    assert err == (
        "operator terminal install refused: the operator terminal setup runs only outside agent sessions, "
        "and engineer@abc-1 is one\n"
    )


@pytest.mark.parametrize("enabled, code", [(True, 0), (False, 1)])
def test_install_command_exit_follows_the_enable(tmp_path, monkeypatch, enabled, code):
    monkeypatch.setattr(operator_setup, "install", lambda: enabled)

    assert run_main(["install"], {}, Who(), tmp_path / "home") == (code, "", "")


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
