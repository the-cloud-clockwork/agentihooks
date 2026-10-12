import stat

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJumpHostKey"


def store():
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def ca_key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = tmp_path / "user_ca"
    path.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        )
    )
    return path


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_jump_sshd_only_forwards_to_worker_terminal_ports(tmp_path):
    from scripts.swarm_v2.kubernetes import jump

    assert jump.sshd_config(tmp_path, "worker").splitlines() == [
        "Port 2222",
        "ListenAddress 0.0.0.0",
        f"HostKey {tmp_path}/host_key",
        "PidFile none",
        "AuthorizedKeysFile none",
        f"TrustedUserCAKeys {tmp_path}/user_ca.pub",
        f"AuthorizedPrincipalsFile {tmp_path}/principals",
        "AllowUsers worker",
        "ForceCommand /bin/false",
        "PermitTTY no",
        "AllowTcpForwarding local",
        "PermitOpen *:2222",
        "AllowStreamLocalForwarding no",
        "AllowAgentForwarding no",
        "X11Forwarding no",
        "PermitTunnel no",
        "GatewayPorts no",
        "PermitRootLogin no",
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "HostbasedAuthentication no",
        "UsePAM no",
        "StrictModes no",
        "PermitUserEnvironment no",
        "PrintMotd no",
        "LogLevel VERBOSE",
    ]


def test_prepare_writes_the_jump_material_privately_and_keeps_its_host_key(tmp_path):
    from cryptography.hazmat.primitives import serialization

    from scripts.swarm_v2.herdr.attach import USER
    from scripts.swarm_v2.kubernetes import jump

    state = tmp_path / "jump" / "state"
    user_ca = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUserCa"

    host_public = jump.prepare(state, user_ca)

    key = serialization.load_ssh_private_key((state / "host_key").read_bytes(), None)
    shown = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    assert host_public == shown.decode()
    assert (state / "user_ca.pub").read_text() == f"{user_ca}\n"
    assert (state / "principals").read_text() == ""
    assert (state / "sshd_config").read_text() == jump.sshd_config(state, USER)
    assert {name: mode(state / name) for name in ("host_key", "user_ca.pub", "principals", "sshd_config")} == {
        "host_key": 0o600,
        "user_ca.pub": 0o600,
        "principals": 0o600,
        "sshd_config": 0o600,
    }
    assert mode(state) == 0o700
    assert jump.prepare(state, user_ca) == host_public


def live(execution_id: str):
    from scripts.swarm_v2.herdr.endpoints import Scope
    from scripts.swarm_v2.herdr.upkeep import Attempt

    return Attempt(Scope("operator/operator", "fixture", execution_id, 1), HOST_KEY)


def test_the_jump_forwards_only_to_the_live_worker_terminals_it_is_given(tmp_path):
    from scripts.swarm_v2.herdr.endpoints import Endpoint
    from scripts.swarm_v2.kubernetes import jump

    one, two = live("exec-a"), live("exec-b")
    jump.Routes(tmp_path, "workers").publish([two, one])

    hosts = [Endpoint(attempt.scope, "workers").host for attempt in (one, two)]
    assert (tmp_path / "principals").read_text() == (
        f'restrict,port-forwarding,permitopen="{hosts[0]}:2222",permitopen="{hosts[1]}:2222" {jump.PRINCIPAL}\n'
    )
    assert "exec-c" not in (tmp_path / "principals").read_text()
    assert mode(tmp_path / "principals") == 0o600


def test_with_no_live_worker_the_jump_admits_nobody(tmp_path):
    from scripts.swarm_v2.kubernetes import jump

    routes = jump.Routes(tmp_path, "workers")
    routes.publish([live("exec-a")])
    routes.publish([])

    assert (tmp_path / "principals").read_text() == ""


def test_routes_are_built_only_when_the_jump_state_is_set(tmp_path):
    from scripts.swarm_v2.kubernetes import jump

    assert jump.STATE_ENV == "AGENTIHOOKS_TERMINAL_JUMP_STATE"
    assert jump.routes({}, "workers") is None
    assert jump.routes({jump.STATE_ENV: ""}, "workers") is None
    built = jump.routes({jump.STATE_ENV: str(tmp_path)}, "workers")
    assert (built.state, built.namespace) == (tmp_path, "workers")


def test_the_controller_records_the_jump_for_the_operator():
    from scripts.swarm_v2.herdr.attach import Jump
    from scripts.swarm_v2.kubernetes import jump

    made = store()
    assert jump.recorded(made) is None

    jump.record(made, Jump("swarm.example", 2223, HOST_KEY))

    assert jump.recorded(made) == Jump("swarm.example", 2223, HOST_KEY)


def test_prepare_command_places_the_material_and_records_the_jump(tmp_path):
    from scripts.swarm_v2.herdr.attach import Jump
    from scripts.swarm_v2.kubernetes import jump, operator_terminal

    made, key = store(), ca_key(tmp_path)
    environ = {"AGENTIHOOKS_TERMINAL_CA_KEY": str(key), "AGENTIHOOKS_TERMINAL_JUMP": "swarm.example:2223"}

    assert jump.main(["prepare", "--state", str(tmp_path / "jump")], environ, lambda: made) == 0

    user_ca = (tmp_path / "jump" / "user_ca.pub").read_text()
    assert user_ca == operator_terminal.authority(environ).user_ca + "\n"
    host_public = jump.prepare(tmp_path / "jump", user_ca.strip())
    assert jump.recorded(made) == Jump("swarm.example", 2223, host_public)


@pytest.mark.parametrize(
    "with_ca, address, message",
    [
        (False, "swarm.example:2223", "opening a worker terminal needs AGENTIHOOKS_TERMINAL_CA_KEY"),
        (True, "", "the controller jump needs AGENTIHOOKS_TERMINAL_JUMP"),
        (True, "swarm.example", "AGENTIHOOKS_TERMINAL_JUMP must be host:port"),
    ],
)
def test_prepare_command_refusals_exit_one_and_record_nothing(tmp_path, capsys, with_ca, address, message):
    from scripts.swarm_v2.kubernetes import jump

    made = store()
    environ = {"AGENTIHOOKS_TERMINAL_JUMP": address}
    if with_ca:
        environ["AGENTIHOOKS_TERMINAL_CA_KEY"] = str(ca_key(tmp_path))

    assert jump.main(["prepare", "--state", str(tmp_path / "jump")], environ, lambda: made) == 1

    assert capsys.readouterr().err == f"controller jump refused: {message}\n"
    assert jump.recorded(made) is None


def test_swarm_jump_is_a_delegated_command():
    from scripts import cli_delegates
    from scripts.swarm_v2.kubernetes import jump

    assert cli_delegates.delegated_cli(["swarm-jump", "prepare"]) is jump.main


def test_parser_names_the_command_and_its_subcommand():
    import argparse
    import io
    from contextlib import redirect_stderr

    from scripts.swarm_v2.kubernetes import jump

    parser = jump._parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    state = next(action for action in sub.choices["prepare"]._actions if action.dest == "state")

    assert (parser.prog, parser.description) == ("agentihooks swarm-jump", "Controller SSH jump to worker terminals")
    assert (sub.dest, sub.required) == ("command", True)
    assert [(choice.dest, choice.help) for choice in sub._choices_actions] == [
        ("prepare", "Write the jump sshd material and record the jump for the operator")
    ]
    assert (state.option_strings, state.required, state.help) == (["--state"], True, "folder the jump sshd reads")
    with pytest.raises(SystemExit), redirect_stderr(io.StringIO()):
        parser.parse_args([])
