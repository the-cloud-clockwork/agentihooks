from pathlib import Path

import pytest

from scripts.swarm import hive_release
from tests.swarm.memory_fixture import tmp_path
from tests.swarm.test_cli import _handoff_doc, env, run
from tests.swarm.test_ready import AGENT, release, released, seated
from tests.swarm.test_stack import park, parked

pytestmark = pytest.mark.unit
_fixture = env, tmp_path, released, parked


def _on_hive(monkeypatch: pytest.MonkeyPatch, member: str) -> None:
    monkeypatch.setattr(hive_release, "member", lambda: member)


def _started(env: tuple, workstation: bool) -> tuple:
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    seated(store, ledger)
    ledger.rows["t1"]["workstation"] = workstation
    return store, ledger


@pytest.mark.feature("hive-workstation-release")
def test_a_hive_handoff_on_a_workstation_task_reopens_it_unclaimed(
    env: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ledger = _started(env, workstation=True)
    _on_hive(monkeypatch, "hive-a")
    assert (ledger.rows["t1"]["claimed_by"], store.claimant("sw", "t1")) == (AGENT, AGENT)
    assert run("sw", "--as", AGENT, "handoff", str(_handoff_doc(tmp_path))) == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert store.claimant("sw", "t1") is None


@pytest.mark.feature("hive-workstation-release")
@pytest.mark.parametrize(("member", "workstation"), [("", True), ("hive-a", False)])
def test_a_handoff_keeps_the_claim_off_a_hive_or_off_a_workstation_task(
    env: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str, workstation: bool
) -> None:
    store, ledger = _started(env, workstation=workstation)
    _on_hive(monkeypatch, member)
    assert run("sw", "--as", AGENT, "handoff", str(_handoff_doc(tmp_path))) == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", AGENT)
    assert store.claimant("sw", "t1") == AGENT


@pytest.mark.feature("hive-workstation-release")
def test_a_hive_park_on_a_workstation_task_reopens_it_unclaimed(parked: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, ledger, _, _, doc = parked
    base = type(ledger).update_task.__get__(ledger)
    ledger.update_task = lambda slug, task, fields, by="swarm", if_state=(), if_match=None: base(
        slug, task, fields, by, if_state, if_match
    )
    ledger.rows["t1"]["workstation"] = True
    _on_hive(monkeypatch, "hive-a")
    assert park(doc) == 0
    row = ledger.rows["t1"]
    assert (row["state"], row["claimed_by"], row["parked_on"]) == ("open", "", ["a"])


@pytest.mark.feature("hive-workstation-release")
def test_a_hive_ready_on_a_workstation_task_leaves_its_pull_request_unclaimed(
    released: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ledger, _, doc = released
    ledger.rows["t1"]["workstation"] = True
    _on_hive(monkeypatch, "hive-a")
    assert release(doc) == 0
    row = ledger.rows["t1"]
    assert (row["state"], row["awaiting"], row["claimed_by"]) == ("pr", "merge", "")


@pytest.mark.feature("hive-workstation-release")
def test_a_workstation_ready_keeps_its_claim(released: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, ledger, _, doc = released
    ledger.rows["t1"]["workstation"] = True
    _on_hive(monkeypatch, "")
    assert release(doc) == 0
    assert ledger.rows["t1"]["claimed_by"] == AGENT


@pytest.mark.feature("hive-workstation-release")
def test_a_release_names_no_claim_it_does_not_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_hive(monkeypatch, "hive-a")
    row = {"state": "claimed", "workstation": True, "claimed_by": "engineer@a1b2c3-0009"}
    assert hive_release.fields(row, AGENT) == {}


@pytest.mark.feature("hive-workstation-release")
def test_a_release_writes_as_the_leaver_guarded_on_its_own_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    class Ledger:
        def update_task(self, slug: str, task_id: str, fields: dict, by: str, if_match: dict) -> None:
            calls.append((slug, task_id, fields, by, if_match))

    _on_hive(monkeypatch, "hive-a")
    row = {"id": "t1", "state": "pr", "workstation": True, "claimed_by": AGENT}
    assert hive_release.release(Ledger(), "sw", row, AGENT) == {"claimed_by": ""}
    assert calls == [("sw", "t1", {"claimed_by": ""}, AGENT, {"claimed_by": AGENT})]
