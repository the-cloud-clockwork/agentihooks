import dataclasses

import pytest

from scripts.swarm_v2.herdr import upkeep
from scripts.swarm_v2.herdr.endpoints import Endpoint, Scope
from scripts.swarm_v2.herdr.reconcile import Report
from scripts.swarm_v2.herdr.upkeep import Attempt, Upkeep
from tests import sv2_hdr03_cases as cases

pytestmark = pytest.mark.unit
FX = cases.fixture()
EXE_A, EXE_B = (entry["execution_id"] for entry in FX["managed"])
HOST_KEY = cases.HOST_KEY


def attempt(execution_id: str, generation: int = 1) -> Attempt:
    return Attempt(Scope("operator/operator", FX["swarm"], execution_id, generation), HOST_KEY)


def endpoint(execution_id: str) -> Endpoint:
    return Endpoint(Scope("operator/operator", FX["swarm"], execution_id, 1), FX["namespace"])


def client(tmp_path, ready=None, addresses=("10.43.12.7",)):
    herdr = cases.Herdr([FX["manual"]], {endpoint(e).target for e in (EXE_A, EXE_B)} if ready is None else ready)
    return cases.client_terminal(tmp_path / "client", herdr, FX["namespace"], addresses), herdr


def sidebar(herdr) -> list[str]:
    return cases._sidebar(herdr)


def test_start_registers_every_attachable_attempt_beside_the_manual_machine(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)

    report = keeper.start([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]
    assert report.refused == {}
    assert report.drift == {"missing": 2}
    assert sidebar(herdr) == ["Local", "devbox", endpoint(EXE_A).name, endpoint(EXE_B).name]
    assert keeper.herdr_registration_drift() == 2
    assert HOST_KEY in terminal.home.known_hosts(endpoint(EXE_A)).read_text()


def test_an_unchanged_set_of_attempts_runs_no_herdr_command(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)
    keeper.start([attempt(EXE_A), attempt(EXE_B)])
    ran = len(herdr.commands)

    assert keeper.change([attempt(EXE_B), attempt(EXE_A)]) is None
    assert len(herdr.commands) == ran
    assert keeper.herdr_registration_drift() == 2


def test_start_reconciles_even_when_the_attempts_are_unchanged(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)
    keeper.start([attempt(EXE_A)])
    herdr.machines = [FX["manual"]]

    report = keeper.start([attempt(EXE_A)])

    assert list(report.registered) == [EXE_A]
    assert report.drift == {"missing": 1}
    assert herdr.adds(endpoint(EXE_A).name) == 2


def test_a_new_attempt_is_registered_on_change(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)
    keeper.start([attempt(EXE_A)])

    report = keeper.change([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]
    assert report.drift == {"missing": 1}
    assert herdr.adds(endpoint(EXE_A).name) == 1
    assert herdr.adds(endpoint(EXE_B).name) == 1


def test_a_new_generation_of_an_attempt_reconciles_again(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)
    keeper.start([attempt(EXE_A)])
    ran = len(herdr.commands)

    report = keeper.change([attempt(EXE_A, 2)])

    assert list(report.registered) == [EXE_A]
    assert len(herdr.commands) > ran
    assert terminal.registrations.store.load()[EXE_A].generation == 2


def test_an_ended_attempt_is_retired_and_the_manual_machine_kept(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)
    keeper.start([attempt(EXE_A), attempt(EXE_B)])

    report = keeper.change([attempt(EXE_A)])

    assert report.retired == [EXE_B]
    assert report.drift == {"retired": 1}
    assert sidebar(herdr) == ["Local", "devbox", endpoint(EXE_A).name]
    assert keeper.herdr_registration_drift() == 1


def test_an_unroutable_attempt_is_refused_and_never_saved(tmp_path):
    terminal, herdr = client(tmp_path, addresses=())
    keeper = Upkeep(terminal)

    report = keeper.start([attempt(EXE_A)])

    assert report.refused == {EXE_A: "no_route"}
    assert report.registered == {}
    assert herdr.mutating() == []


def test_a_refused_attempt_is_retried_on_the_next_change(tmp_path):
    terminal, herdr = client(tmp_path, ready={endpoint(EXE_A).target})
    keeper = Upkeep(terminal)
    assert keeper.start([attempt(EXE_A), attempt(EXE_B)]).refused == {EXE_B: "machine_refused"}
    herdr.ready.add(endpoint(EXE_B).target)

    report = keeper.change([attempt(EXE_A), attempt(EXE_B)])

    assert sorted(report.registered) == [EXE_A, EXE_B]
    assert report.refused == {}
    assert herdr.adds(endpoint(EXE_B).name) == 2
    assert keeper.change([attempt(EXE_A), attempt(EXE_B)]) is None


def test_an_unreadable_client_names_it_and_retries_on_the_next_change(tmp_path):
    terminal, herdr = client(tmp_path)
    keeper = Upkeep(terminal)
    keeper.start([attempt(EXE_A)])
    herdr.listing = "{"

    assert keeper.change([attempt(EXE_A), attempt(EXE_B)]) == Report(refused={"herdr": "catalog_unreadable"})
    assert keeper.herdr_registration_drift() == 1
    herdr.listing = None
    report = keeper.change([attempt(EXE_A)])

    assert list(report.registered) == [EXE_A]
    assert report.drift == {}


def test_a_fresh_upkeep_reconciles_even_an_empty_set_of_attempts(tmp_path):
    terminal, herdr = client(tmp_path)

    report = Upkeep(terminal).change([])

    assert report == Report()
    assert herdr.commands == [["herdr", "machine", "list", "--json"]]


def test_an_upkeep_that_has_not_reconciled_reports_no_drift(tmp_path):
    terminal, herdr = client(tmp_path)

    assert Upkeep(terminal).herdr_registration_drift() == 0
    assert herdr.commands == []


def test_an_attempt_is_a_frozen_value():
    with pytest.raises(dataclasses.FrozenInstanceError):
        attempt(EXE_A).host_public = "ssh-ed25519 AAAA-other"


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
