from scripts.events import nkey
from scripts.swarm import controller


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
    assert capsys.readouterr().err == "controller: bus nats unreachable as swarm-controller: connection refused\n"
