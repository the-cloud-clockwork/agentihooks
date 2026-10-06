import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli as swarm_cli
from scripts.swarm import phase_state
from scripts.swarm.store import RedisStore
from tests.doctor.test_doctor_cli import FileLedger, core, new_ledger, state
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG = "phased"
MASTER_SEAT = f"master@{SLUG}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    content = {
        "title": "Phased work",
        "overview": "o",
        "phases": [{"title": "Build", "description": "d"}, {"title": "Empty", "description": "d"}],
    }
    assert new_ledger.create(SLUG, content)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(swarm_cli, "connect", lambda: store)
    monkeypatch.setattr(swarm_cli, "LedgerClient", FileLedger)
    monkeypatch.setattr(swarm_cli, "HerdrRuntime", FakeRuntime)
    monkeypatch.setattr(swarm_cli.timer, "ensure", lambda binary: True)
    assert swarm_cli.main([SLUG, "create", "--repo", "/repo", "--max-eng-agents", "0"]) == 0
    ledger = FileLedger()
    for task in ("t1", "t2"):
        ledger.add_task(SLUG, {"task": task, "title": f"Task {task[1]}", "lane": "eng", "phase": "p1"}, "init-swarm")
    return store, ledger


def run(store, ledger):
    return swarm_cli.run_tick(store, SLUG, ledger, FakeRuntime(), FakeHerdr({}))


def phase(pid):
    return next(p for p in state(SLUG)["phases"] if p["id"] == pid)


def master_texts(store):
    return [i.text for i in InboxStore(store.redis).inbox(MASTER_SEAT)]


def finish(ledger, *task_ids):
    for task in task_ids:
        ledger.update_task(SLUG, task, {"state": "done"})


def test_a_phase_whose_tasks_are_all_done_is_ticked_with_a_status_and_the_master_told(env):
    store, ledger = env
    finish(ledger, "t1", "t2")
    actions = run(store, ledger)
    p1 = phase("p1")
    assert p1["done"] is True
    assert [c["by"] for c in p1["comments"]] == ["swarm"]
    assert "every task" in p1["comments"][0]["text"].lower()
    assert any("Build" in t and "done" in t for t in master_texts(store))
    assert any("p1" in a for a in actions)


def test_a_phase_with_one_open_task_stays_open_and_nothing_is_sent(env):
    store, ledger = env
    finish(ledger, "t1")
    run(store, ledger)
    assert phase("p1")["done"] is False
    assert phase("p1")["comments"] == []
    assert not any("Build" in t for t in master_texts(store))


def test_a_phase_without_tasks_is_left_as_it_is(env):
    store, ledger = env
    finish(ledger, "t1", "t2")
    run(store, ledger)
    assert phase("p2")["done"] is False
    assert phase("p2")["comments"] == []
    assert not any("Empty" in t for t in master_texts(store))


def test_a_new_open_task_reopens_a_done_phase_with_a_status_and_the_master_told(env):
    store, ledger = env
    finish(ledger, "t1", "t2")
    run(store, ledger)
    ledger.add_task(SLUG, {"task": "t3", "title": "Task three", "lane": "eng", "phase": "p1"}, "sw-master-1")
    run(store, ledger)
    p1 = phase("p1")
    assert p1["done"] is False
    assert "reopened" in p1["comments"][-1]["text"].lower()
    assert sum("Build" in t for t in master_texts(store)) == 2


def test_a_ticked_phase_is_not_ticked_again_on_the_next_tick(env):
    store, ledger = env
    finish(ledger, "t1", "t2")
    run(store, ledger)
    run(store, ledger)
    assert sum("Build" in t for t in master_texts(store)) == 1


def test_phase_notices_to_the_master_are_informational(env):
    store, ledger = env
    finish(ledger, "t1", "t2")
    run(store, ledger)
    ledger.update_task(SLUG, "t2", {"state": "claimed"})
    run(store, ledger)
    assert [i.fyi for i in InboxStore(store.redis).inbox(MASTER_SEAT) if "phase p1" in i.text] == [True, True]


def test_a_finished_phase_opens_its_waiting_phase_before_the_drained_decision(env):
    store, ledger = env
    op = {
        "op": "phase_update",
        "id": "wait-on-build",
        "by": "engineer",
        "item": "phases/p2",
        "fields": {"depends_on": ["p1"]},
    }
    assert core.sync(SLUG, ops=[op])[1] == []
    ledger.add_task(SLUG, {"task": "t3", "title": "Task three", "lane": "eng", "phase": "p2"}, "init-swarm")
    store.update(SLUG, state="running")
    finish(ledger, "t1", "t2")
    rt = FakeRuntime()
    actions = swarm_cli.run_tick(store, SLUG, ledger, rt, FakeHerdr({}))
    doc = state(SLUG)
    assert phase("p1")["done"] is True
    assert phase_state.lifecycle(phase("p2"), doc) == "building"
    assert "drained" not in actions and store.config(SLUG).state == "running"
    assert not any("no task left" in c["text"] for c in doc["chat"])
    assert rt.killed == [] and rt.closed_spaces == []
    assert [a.lane for a in store.agents(SLUG)] == ["master"]
