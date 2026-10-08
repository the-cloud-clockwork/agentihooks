import datetime
import json
from pathlib import Path

import pytest

from scripts.swarm import naming
from scripts.swarm.store import DEFAULT_URL
from scripts.swarm_ledger import ledger_creator, new_ledger
from scripts.swarm_ledger.repository import repository

pytestmark = pytest.mark.unit

SWARM_KEYS = ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME")
ASKED = "please make a two task ledger for the hotfix"


def shared():
    return Path.home() / "development-ledger"


def as_lane(monkeypatch, lane, name=None):
    for key in SWARM_KEYS:
        monkeypatch.delenv(key, raising=False)
    if lane != "operator":
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
        monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", lane)
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name or f"{lane}@a1b2c3-0001")


def content_file(tmp_path, phases):
    content = {"title": "Plan", "overview": "o", "phases": [{"title": f"step {n}"} for n in range(phases)]}
    path = tmp_path / "content.json"
    path.write_text(json.dumps(content))
    return path


def new(monkeypatch, tmp_path, folder, content, *extra, size="small"):
    monkeypatch.setenv("LEDGER_DIR", str(folder))
    monkeypatch.setattr(new_ledger.core, "LEDGER_DIR", folder)
    monkeypatch.setattr(new_ledger.ledger_link, "serving", lambda: str(folder))
    plan = tmp_path / "creator-plan.md"
    plan.write_text("plan")
    if isinstance(content, int):
        content = content_file(tmp_path, content)
    argv = ["new", "--content", str(content), "--plan", str(plan), "--size", size, "--as", "tester", *extra]
    monkeypatch.setattr("sys.argv", argv)
    new_ledger.main()
    slug = naming.plan_slug(plan, datetime.date.today().isoformat())
    return repository.get_document(slug)


def created(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[0])["created"] is True


@pytest.mark.parametrize("lane", ["eng", "ci", "plan", ""])
def test_a_swarm_engineer_cannot_create_a_ledger_in_the_shared_folder(monkeypatch, tmp_path, lane):
    as_lane(monkeypatch, lane)
    with pytest.raises(SystemExit) as refused:
        new(monkeypatch, tmp_path, shared(), 3)
    assert str(refused.value) == ledger_creator.CALLER
    slug = naming.plan_slug(tmp_path / "creator-plan.md", datetime.date.today().isoformat())
    assert not repository.exists(slug)


@pytest.mark.parametrize("lane", ["master", "operator"])
def test_a_master_or_the_operator_creates_a_small_ledger_of_three_tasks(monkeypatch, tmp_path, capsys, lane):
    as_lane(monkeypatch, lane)
    assert new(monkeypatch, tmp_path, shared(), 3)["size"] == "small"
    assert created(capsys)


@pytest.mark.parametrize("lane", ["master", "operator"])
def test_fewer_than_three_tasks_is_refused_unless_the_operator_asked(monkeypatch, tmp_path, lane):
    as_lane(monkeypatch, lane)
    with pytest.raises(SystemExit) as refused:
        new(monkeypatch, tmp_path, shared(), 2)
    assert str(refused.value) == ledger_creator.FLOOR.format(need=3, have=2)


def test_content_that_is_not_an_object_counts_as_no_tasks(monkeypatch, tmp_path):
    as_lane(monkeypatch, "master")
    listed = tmp_path / "listed.json"
    listed.write_text("[]")
    with pytest.raises(SystemExit) as refused:
        new(monkeypatch, tmp_path, shared(), listed)
    assert str(refused.value) == ledger_creator.FLOOR.format(need=3, have=0)


def test_a_swarm_ledger_leaves_the_floor_to_swarm_create_where_its_tasks_exist(monkeypatch, tmp_path, capsys):
    as_lane(monkeypatch, "master")
    assert new(monkeypatch, tmp_path, shared(), 2, size="swarm")["size"] == "swarm"
    assert created(capsys)


@pytest.mark.parametrize("quote", ["make a two task ledger", "two task ledger"])
def test_the_operator_words_recorded_for_the_master_lift_the_floor(monkeypatch, tmp_path, capsys, quote):
    from hooks.context import operator_words

    as_lane(monkeypatch, "master")
    operator_words.record("master@a1b2c3-0001", ASKED)
    new(monkeypatch, tmp_path, shared(), 2, "--operator-asked", quote)
    assert created(capsys)


@pytest.mark.parametrize("quote", ["words the operator never typed", "two task", ""])
def test_a_quote_the_operator_did_not_type_keeps_the_floor(monkeypatch, tmp_path, quote):
    from hooks.context import operator_words

    as_lane(monkeypatch, "master")
    operator_words.record("master@a1b2c3-0001", ASKED)
    with pytest.raises(SystemExit) as refused:
        new(monkeypatch, tmp_path, shared(), 2, "--operator-asked", quote)
    assert str(refused.value) == ledger_creator.FLOOR.format(need=3, have=2)


def test_the_operator_own_unnamed_session_lifts_the_floor_with_his_words(monkeypatch, tmp_path, capsys):
    as_lane(monkeypatch, "operator")
    new(monkeypatch, tmp_path, shared(), 1, "--operator-asked", "one step ledger please")
    assert created(capsys)


def test_an_unnamed_swarm_session_cannot_lift_the_floor():
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "master"}
    assert ledger_creator.operator_asked(environ, "one step ledger please") is False


@pytest.mark.parametrize("redis", [None, "redis://127.0.0.1:6390/0"])
def test_a_proof_ledger_runs_on_a_scratch_folder_and_a_spare_port(monkeypatch, tmp_path, capsys, redis):
    as_lane(monkeypatch, "eng")
    monkeypatch.setenv("LEDGER_PORT", "8883")
    if redis is None:
        monkeypatch.delenv("AGENTIHOOKS_SWARM_REDIS_URL")
    else:
        monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", redis)
    new(monkeypatch, tmp_path, tmp_path / "scratch", 1)
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0])["created"] is True
    assert "http://127.0.0.1:8883/" in out[1]


@pytest.mark.parametrize("port", ["8765", None])
def test_a_data_folder_on_the_default_port_creates_its_ledger(monkeypatch, tmp_path, capsys, port):
    as_lane(monkeypatch, "eng")
    if port is None:
        monkeypatch.delenv("LEDGER_PORT", raising=False)
    else:
        monkeypatch.setenv("LEDGER_PORT", port)
    new(monkeypatch, tmp_path, tmp_path / "data", 3)
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0])["created"] is True
    assert "http://127.0.0.1:8765/" in out[1]


@pytest.mark.parametrize("lane", ["eng", "ci", "plan"])
def test_the_home_folder_on_another_port_keeps_the_shared_folder_rule(monkeypatch, tmp_path, lane):
    as_lane(monkeypatch, lane)
    monkeypatch.setenv("LEDGER_PORT", "8883")
    with pytest.raises(SystemExit) as refused:
        new(monkeypatch, tmp_path, shared(), 3)
    assert str(refused.value) == ledger_creator.CALLER
    slug = naming.plan_slug(tmp_path / "creator-plan.md", datetime.date.today().isoformat())
    assert not repository.exists(slug)


def test_the_master_in_the_home_folder_on_another_port_gets_the_pinned_port(monkeypatch, tmp_path, capsys):
    as_lane(monkeypatch, "master")
    monkeypatch.setenv("LEDGER_PORT", "8883")
    new(monkeypatch, tmp_path, shared(), 3)
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0])["created"] is True
    assert "http://127.0.0.1:8765/" in out[1]


@pytest.mark.parametrize("pinned", [True, False])
def test_the_creator_reads_the_server_folder_rule(monkeypatch, pinned):
    seen = []

    def shared_directory(environ):
        seen.append(environ)
        return pinned

    monkeypatch.setattr("scripts.swarm_ledger.ledger_link.shared_directory", shared_directory)
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "eng", "LEDGER_DIR": "/data", "LEDGER_PORT": "8765"}
    assert ledger_creator.creator_refusal(environ) == (ledger_creator.CALLER if pinned else "")
    assert seen == [environ]


@pytest.mark.parametrize("redis", [None, "", DEFAULT_URL])
def test_a_proof_swarm_needs_its_own_redis(tmp_path, redis):
    environ = {"LEDGER_DIR": str(tmp_path), "LEDGER_PORT": "8883"}
    if redis is not None:
        environ["AGENTIHOOKS_SWARM_REDIS_URL"] = redis
    assert ledger_creator.swarm_refusal(environ) == ledger_creator.REDIS


@pytest.mark.parametrize(
    "environ, refusal",
    [
        ({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "eng"}, ledger_creator.CALLER),
        ({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "master"}, ""),
        (
            {"LEDGER_DIR": "/scratch", "LEDGER_PORT": "8765", "AGENTIHOOKS_SWARM_REDIS_URL": "redis://x:1/0"},
            "",
        ),
        ({"LEDGER_DIR": "/scratch", "LEDGER_PORT": "8765"}, ledger_creator.REDIS),
        ({"LEDGER_DIR": "/scratch", "LEDGER_PORT": "8883", "AGENTIHOOKS_SWARM_REDIS_URL": "redis://x:1/0"}, ""),
    ],
)
def test_a_swarm_takes_the_ledger_rules_before_its_own_redis(environ, refusal):
    assert ledger_creator.swarm_refusal(environ) == refusal


def test_without_a_ledger_folder_the_shared_one_is_judged():
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "eng"}
    assert ledger_creator.creator_refusal(environ) == ledger_creator.CALLER


def test_swarm_tasks_count_each_waiting_automatic_phase():
    doc = {
        "tasks": [{"id": "t1", "phase": "p1"}, {"id": "t2", "phase": "p2"}],
        "phases": [
            {"id": "p1", "planning": "auto"},
            {"id": "p2", "planning": "manual"},
            {"id": "p3", "planning": "auto"},
            {"id": "p4", "planning": "auto"},
            {"id": "p5"},
        ],
    }
    assert ledger_creator.swarm_tasks(doc) == 4


def test_swarm_tasks_read_a_ledger_missing_tasks_or_phases():
    assert ledger_creator.swarm_tasks({"phases": [{"id": "p1", "planning": "auto"}]}) == 1
    assert ledger_creator.swarm_tasks({"tasks": [{"id": "t1"}]}) == 1
