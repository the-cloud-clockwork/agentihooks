import pytest

from scripts.swarm import hive_release
from tests.swarm.memory_fixture import tmp_path
from tests.swarm.test_cli import _handoff_doc, env, run
from tests.swarm.test_ready import AGENT, release, released

pytestmark = pytest.mark.unit
_fixture = env, tmp_path, released


def _on_hive(monkeypatch: pytest.MonkeyPatch, member: str) -> None:
    monkeypatch.setattr(hive_release, "member", lambda: member)


def _started(env: tuple, workstation: bool) -> tuple:
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.rows["t1"]["workstation"] = workstation
    return store, ledger


@pytest.mark.feature("hive-workstation-release")
def test_a_hive_handoff_on_a_workstation_task_reopens_it_unclaimed(env, tmp_path, monkeypatch):
    _, ledger = _started(env, workstation=True)
    _on_hive(monkeypatch, "hive-a")
    assert ledger.rows["t1"]["claimed_by"] == AGENT
    assert run("sw", "--as", AGENT, "handoff", str(_handoff_doc(tmp_path))) == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")


@pytest.mark.feature("hive-workstation-release")
@pytest.mark.parametrize(("member", "workstation"), [("", True), ("hive-a", False)])
def test_a_handoff_keeps_the_claim_off_a_hive_or_off_a_workstation_task(
    env, tmp_path, monkeypatch, member, workstation
):
    _, ledger = _started(env, workstation=workstation)
    _on_hive(monkeypatch, member)
    assert run("sw", "--as", AGENT, "handoff", str(_handoff_doc(tmp_path))) == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", AGENT)


@pytest.mark.feature("hive-workstation-release")
def test_a_hive_ready_on_a_workstation_task_leaves_its_pull_request_unclaimed(released, monkeypatch):
    _, ledger, _, doc = released
    ledger.rows["t1"]["workstation"] = True
    _on_hive(monkeypatch, "hive-a")
    assert release(doc) == 0
    row = ledger.rows["t1"]
    assert (row["state"], row["awaiting"], row["claimed_by"]) == ("pr", "merge", "")


@pytest.mark.feature("hive-workstation-release")
def test_a_workstation_ready_keeps_its_claim(released, monkeypatch):
    _, ledger, _, doc = released
    ledger.rows["t1"]["workstation"] = True
    _on_hive(monkeypatch, "")
    assert release(doc) == 0
    assert ledger.rows["t1"]["claimed_by"] == AGENT


@pytest.mark.feature("hive-workstation-release")
def test_a_release_names_no_claim_it_does_not_hold(monkeypatch):
    _on_hive(monkeypatch, "hive-a")
    row = {"state": "claimed", "workstation": True, "claimed_by": "engineer@a1b2c3-0009"}
    assert hive_release.fields(row, AGENT) == {}
