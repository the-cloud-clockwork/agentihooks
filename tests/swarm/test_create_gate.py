from pathlib import Path

import pytest

from scripts.doctor import priming
from scripts.swarm import cli, templates
from scripts.swarm_ledger import ledger_creator
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run

pytestmark = pytest.mark.unit


def as_lane(monkeypatch, lane, folder=None):
    monkeypatch.setenv("LEDGER_DIR", str(folder or Path.home() / "development-ledger"))
    for key in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_AGENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    if lane != "operator":
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "other")
        monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", lane)
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", f"{lane}@ffffff-0001")


def refused(capsys, message):
    return capsys.readouterr().err.strip() == f"swarm: {message}"


@pytest.mark.parametrize("lane", ["eng", "ci", "plan"])
def test_a_swarm_engineer_cannot_create_a_swarm_on_the_shared_ledger_folder(env, monkeypatch, capsys, lane):
    store, ledger, _ = env
    ledger.rows.update({"t3": {"id": "t3", "lane": "eng", "state": "open"}})
    as_lane(monkeypatch, lane)
    assert run("sw", "create", "--repo", "/repo") == 1
    assert refused(capsys, ledger_creator.CALLER) and "sw" not in store.slugs()


def test_a_master_swarm_of_two_tasks_needs_the_operator_to_ask(env, monkeypatch, capsys):
    store, _, _ = env
    as_lane(monkeypatch, "master")
    assert run("sw", "create", "--repo", "/repo") == 1
    assert refused(capsys, ledger_creator.FLOOR.format(need=3, have=2)) and "sw" not in store.slugs()


def test_the_operator_words_recorded_for_the_master_lift_the_swarm_floor(env, monkeypatch):
    from hooks.context import operator_words

    store, _, _ = env
    as_lane(monkeypatch, "master")
    operator_words.record("master@ffffff-0001", "go ahead with a two task swarm")
    assert run("sw", "create", "--repo", "/repo", "--operator-asked", "a two task swarm") == 0
    assert "sw" in store.slugs()


@pytest.mark.parametrize("lane", ["master", "operator"])
def test_a_waiting_automatic_phase_counts_toward_three_tasks(env, monkeypatch, lane):
    store, ledger, _ = env
    ledger.phases = [{"id": "p1", "planning": "manual"}, {"id": "p2", "planning": "auto"}]
    as_lane(monkeypatch, lane)
    assert run("sw", "create", "--repo", "/repo") == 0 and "sw" in store.slugs()


def test_a_proof_swarm_on_a_scratch_folder_needs_its_own_redis(env, monkeypatch, capsys, tmp_path):
    store, _, _ = env
    as_lane(monkeypatch, "eng", tmp_path / "scratch")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_REDIS_URL")
    assert run("sw", "create", "--repo", "/repo") == 1
    assert refused(capsys, ledger_creator.REDIS) and "sw" not in store.slugs()


def test_a_proof_swarm_on_a_scratch_folder_spare_port_and_own_redis_is_created(env, monkeypatch, tmp_path):
    store, _, _ = env
    as_lane(monkeypatch, "eng", tmp_path / "scratch")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://127.0.0.1:6390/0")
    assert run("sw", "create", "--repo", "/repo") == 0 and "sw" in store.slugs()


def test_a_doctor_crew_needs_a_master_or_the_operator_but_no_task_floor(env, monkeypatch, capsys):
    store, ledger, _ = env
    ledger.rows.clear()
    monkeypatch.setattr(cli.templates, "load", lambda name, environ: templates.parse({"name": name}))
    as_lane(monkeypatch, "master")
    assert run("sw", "create", "--repo", "/repo", "--template", priming.TEMPLATE) == 0
    as_lane(monkeypatch, "eng")
    assert run("sx", "create", "--repo", "/repo", "--template", priming.TEMPLATE) == 1
    assert refused(capsys, ledger_creator.CALLER) and "sx" not in store.slugs()
