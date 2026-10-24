import pytest

from scripts import operator_env
from scripts.events import nkey
from scripts.swarm import controller
from scripts.swarm_v2 import control_service, proof_swarm

pytestmark = pytest.mark.unit


def creds(name):
    account, user = nkey.create(nkey.ACCOUNT), nkey.create(nkey.USER)
    return nkey.creds(nkey.encode_jwt(account, user.public, name, {"type": "user"}), user.seed)


def test_controller_opens_the_nats_bus_without_provisioning_and_logs_its_user(capsys):
    opened = []
    environ = {
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": "tls://bus:4222",
        "AGENTIHOOKS_BUS_CREDENTIALS": creds("swarm-controller"),
    }
    bus = controller.open_controller_bus(environ, lambda config: opened.append(config) or "bus")
    assert bus == "bus"
    assert [(item.backend, item.url, item.provision) for item in opened] == [("nats", "tls://bus:4222", False)]
    assert capsys.readouterr().out == "controller: bus nats at tls://bus:4222 as swarm-controller\n"


def test_controller_without_a_nats_bus_opens_nothing(capsys):
    assert controller.open_controller_bus({}, lambda config: "bus") is None
    assert capsys.readouterr().out == ""


def test_controller_keeps_running_when_the_bus_refuses(capsys):
    def refused(config):
        raise OSError("connection refused")

    environ = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_CREDENTIALS": creds("swarm-controller")}
    assert controller.open_controller_bus(environ, refused) is None
    assert capsys.readouterr().err == "controller: bus nats refused: connection refused\n"


def test_controller_keeps_running_on_a_malformed_bus_credential(capsys):
    environ = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_CREDENTIALS": "-----BEGIN NATS USER JWT-----\nbroken\n"}
    assert controller.open_controller_bus(environ, lambda config: "bus") is None
    assert (
        capsys.readouterr().err == "controller: bus nats refused: a NATS creds file holds one user JWT and one seed\n"
    )


def test_controller_keeps_running_when_its_bus_credential_cannot_be_read(capsys, tmp_path):
    missing = tmp_path / "missing.creds"
    environ = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_CREDENTIALS": str(missing)}
    assert controller.open_controller_bus(environ, lambda config: "bus") is None
    err = capsys.readouterr().err
    assert err.startswith("controller: bus nats refused: [Errno 2] No such file or directory")
    assert str(missing) in err


def test_controller_bus_without_credentials_signs_in_anonymously(capsys):
    environ = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://bus:4222"}
    assert controller.open_controller_bus(environ, lambda config: "bus") == "bus"
    assert capsys.readouterr().out == "controller: bus nats at nats://bus:4222 as anonymous\n"


def test_controller_bus_lines_flush_so_the_pod_log_shows_them_at_once(monkeypatch):
    printed = []
    monkeypatch.setattr(controller, "print", lambda *args, **kwargs: printed.append(kwargs), raising=False)
    environ = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://bus:4222"}
    controller.open_controller_bus(environ, lambda config: "bus")
    controller.open_controller_bus({**environ, "AGENTIHOOKS_BUS_CREDENTIALS": "-----BEGIN NATS USER JWT-----\n"}, None)
    assert printed == [{"flush": True}, {"file": controller.sys.stderr, "flush": True}]


class Bus:
    closed = 0

    def close(self):
        self.closed += 1


def test_controller_run_holds_its_bus_open_through_the_ticks_and_closes_it_on_exit(monkeypatch):
    bus, seen = Bus(), []
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(controller, "open_controller_bus", lambda env: bus if env is controller.os.environ else None)
    monkeypatch.setattr(proof_swarm, "run", lambda env, store, require=None: None)
    monkeypatch.setattr(control_service, "host", lambda env, store, hive: None)
    monkeypatch.setattr(controller, "tick_once", lambda store, service: seen.append(bus.closed) or {})
    assert controller.main(["run", "--once"]) == 0
    assert (seen, bus.closed) == ([0], 1)
