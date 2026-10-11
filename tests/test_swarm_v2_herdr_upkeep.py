import socket

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.swarm_v2.herdr.upkeep import Attempt, Upkeep

from scripts.swarm_v2.herdr import upkeep
from scripts.swarm_v2.herdr.attach import ClientHome, Terminal
from scripts.swarm_v2.herdr.endpoints import Endpoint, Scope, TerminalAuthority
from scripts.swarm_v2.herdr.reconcile import Report
from tests import sv2_hdr03_cases as cases

pytestmark = pytest.mark.unit
FX = cases.fixture()
EXE_A, EXE_B = (entry["execution_id"] for entry in FX["managed"])
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey"


def attempt(execution_id: str, generation: int = 1) -> Attempt:
    return Attempt(Scope("controller", FX["swarm"], execution_id, generation), HOST_KEY)


def endpoint(execution_id: str) -> Endpoint:
    return Endpoint(Scope("controller", FX["swarm"], execution_id, 1), FX["namespace"])


def client(tmp_path, ready=None, addresses=("10.43.12.7",)):
    herdr = cases.Herdr([FX["manual"]], {endpoint(e).target for e in (EXE_A, EXE_B)} if ready is None else ready)
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: 1_800_000_000)

    def lookup(host, port, type):  # noqa: A002
        return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]

    return Terminal(ClientHome(tmp_path / "client"), authority, FX["namespace"], herdr, lookup), herdr


def sidebar(herdr) -> list[str]:
    return cases._sidebar(herdr)


def test_start_registers_every_attachable_attempt_beside_the_manual_machine(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)

    report = keeper.start([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]
    assert report.refused == {}
    assert report.drift == {"missing": 2}
    assert sidebar(herdr) == ["Local", "devbox", endpoint(EXE_A).name, endpoint(EXE_B).name]
    assert keeper.herdr_registration_drift() == 2


def test_an_unchanged_set_of_attempts_runs_no_herdr_command(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)
    keeper.start([attempt(EXE_A), attempt(EXE_B)])
    ran = len(herdr.commands)

    assert keeper.change([attempt(EXE_B), attempt(EXE_A)]) is None
    assert len(herdr.commands) == ran
    assert keeper.herdr_registration_drift() == 2


def test_start_reconciles_even_when_the_attempts_are_unchanged(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)
    keeper.start([attempt(EXE_A)])
    herdr.machines = [FX["manual"]]

    report = keeper.start([attempt(EXE_A)])

    assert list(report.registered) == [EXE_A]
    assert report.drift == {"missing": 1}
    assert herdr.adds(endpoint(EXE_A).name) == 2


def test_a_new_attempt_is_registered_on_change(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)
    keeper.start([attempt(EXE_A)])

    report = keeper.change([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]
    assert report.drift == {"missing": 1}
    assert herdr.adds(endpoint(EXE_A).name) == 1
    assert herdr.adds(endpoint(EXE_B).name) == 1


def test_a_new_generation_of_an_attempt_reconciles_again(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)
    keeper.start([attempt(EXE_A)])
    ran = len(herdr.commands)

    report = keeper.change([attempt(EXE_A, 2)])

    assert list(report.registered) == [EXE_A]
    assert len(herdr.commands) > ran
    assert terminal.registrations.store.load()[EXE_A].generation == 2


def test_an_ended_attempt_is_retired_and_the_manual_machine_kept(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)
    keeper.start([attempt(EXE_A), attempt(EXE_B)])

    report = keeper.change([attempt(EXE_A)])

    assert report.retired == [EXE_B]
    assert report.drift == {"retired": 1}
    assert sidebar(herdr) == ["Local", "devbox", endpoint(EXE_A).name]
    assert keeper.herdr_registration_drift() == 1


def test_an_unroutable_attempt_is_refused_and_never_saved(tmp_path):
    terminal, herdr = client(tmp_path, addresses=())
    keeper = Upkeep(terminal, True)

    report = keeper.start([attempt(EXE_A)])

    assert report.refused == {EXE_A: "no_route"}
    assert report.registered == {}
    assert herdr.mutating() == []


def test_a_refused_attempt_is_retried_on_the_next_change(tmp_path):
    terminal, herdr = client(tmp_path, ready={endpoint(EXE_A).target})
    keeper = Upkeep(terminal, True)
    assert keeper.start([attempt(EXE_A), attempt(EXE_B)]).refused == {EXE_B: "machine_refused"}
    herdr.ready.add(endpoint(EXE_B).target)

    report = keeper.change([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]
    assert report.refused == {}
    assert herdr.adds(endpoint(EXE_B).name) == 2
    assert keeper.change([attempt(EXE_A), attempt(EXE_B)]) is None


def test_an_unreadable_client_names_it_and_retries_on_the_next_change(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal, True)
    keeper.start([attempt(EXE_A)])
    herdr.listing = "{"

    assert keeper.change([attempt(EXE_A), attempt(EXE_B)]) == Report(refused={"herdr": "catalog_unreadable"})
    assert keeper.herdr_registration_drift() == 1
    herdr.listing = None
    report = keeper.change([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]


def test_switched_off_no_herdr_command_runs_and_no_client_file_is_written(tmp_path):
    terminal, herdr = client(tmp_path)
    owned = {"id": "p-owned", "label": endpoint(EXE_A).name, "target": endpoint(EXE_A).target}
    herdr.machines.append(owned | {"session": "default", "enabled": True})
    before = [dict(machine) for machine in herdr.machines]
    keeper = Upkeep(terminal, False)

    assert keeper.start([attempt(EXE_B)]) is None
    assert keeper.change([]) is None
    assert herdr.commands == []
    assert herdr.machines == before
    assert not (tmp_path / "client").exists()
    assert keeper.herdr_registration_drift() == 0


@pytest.mark.parametrize(
    ("environ", "on"),
    [
        ({}, True),
        ({"AGENTIHOOKS_HERDR_REGISTRATIONS": "on"}, True),
        ({"AGENTIHOOKS_HERDR_REGISTRATIONS": "off"}, False),
    ],
)
def test_the_switch_is_on_unless_set_off(environ, on):
    assert upkeep.SWITCH == "AGENTIHOOKS_HERDR_REGISTRATIONS"
    assert upkeep.switched_on(environ) is on
