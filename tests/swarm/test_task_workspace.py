from pathlib import Path

import pytest

from scripts.swarm import prompt
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from scripts.swarm_ledger import ledger_workspace, plan_read
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

CONTRACT = {"must": "the folder exists", "check": "ls", "judge": "the master"}


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return s


def ledger():
    return FakeLedger([{"id": "t1", "title": "Build it", "description": "Seams here.", "contract": CONTRACT}])


def test_a_claim_creates_the_work_folder_once_and_stores_its_path(store):
    rows, runtime = ledger(), FakeRuntime()
    tick("sw", store, rows, runtime, now_ms=1_000)
    folder = Path.home() / ".agentihooks" / "swarm" / "sw" / "tasks" / "t1"
    assert sorted(p.name for p in folder.iterdir()) == ["progress.md", "proof.md", "steering.md"]
    steering = (folder / "steering.md").read_text(encoding="utf-8")
    for text in ("Build it", "Seams here.", "Must be true: the folder exists", "Checked by: ls"):
        assert text in steering
    assert rows.rows["t1"]["workspace"] == str(folder)
    assert runtime.tasks[0]["workspace"] == str(folder)


def test_a_reclaim_reuses_the_folder_and_keeps_what_is_there(store):
    rows, runtime = ledger(), FakeRuntime()
    tick("sw", store, rows, runtime, now_ms=1_000)
    folder = Path(rows.rows["t1"]["workspace"])
    (folder / "progress.md").write_text("tests red\n", encoding="utf-8")
    (folder / "steering.md").write_text("operator steer\n", encoding="utf-8")
    store.release("sw", "t1", "engineer@a1b2c3-0001")
    store.drop_agent("sw", "engineer@a1b2c3-0001")
    rows.rows["t1"].update(state="open", claimed_by="", description="changed")
    tick("sw", store, rows, runtime, now_ms=2_000)
    assert [t["workspace"] for t in runtime.tasks] == [str(folder), str(folder)]
    assert (folder / "progress.md").read_text(encoding="utf-8") == "tests red\n"
    assert (folder / "steering.md").read_text(encoding="utf-8") == "operator steer\n"


def test_the_prompt_names_the_work_folder():
    task = {"id": "t1", "title": "Build it", "workspace": "/w/sw/tasks/t1"}
    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", task)
    assert "/w/sw/tasks/t1" in text
    for name in ("steering.md", "progress.md", "proof.md"):
        assert name in text


def test_a_prompt_without_a_work_folder_says_nothing_about_one():
    assert "steering.md" not in prompt.build(
        "sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "Build it"}
    )


SLICED = {"id": "t1", "title": "Build it", "plan_url": "https://example.com/plan", "plan_lines": "4-9", "phase": "p1"}


def test_steering_names_the_plan_read_command_in_place_of_the_plan_link():
    assert ledger_workspace.steering(SLICED) == f"# t1: Build it\n\n{plan_read.pointer(SLICED)}\n"


def test_steering_keeps_the_plan_link_for_a_task_without_plan_lines():
    task = {"id": "t1", "title": "Build it", "plan_url": "https://example.com/plan"}
    assert "Plan: https://example.com/plan" in ledger_workspace.steering(task).splitlines()


def test_the_prompt_names_the_plan_read_command_for_a_sliced_task():
    lines = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", SLICED).splitlines()
    assert plan_read.pointer(SLICED) in lines


def test_a_prompt_for_a_task_without_plan_lines_says_nothing_about_plan_read():
    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "Build it"})
    assert plan_read.COMMAND not in text


def test_tails_return_the_latest_progress_and_proof_lines():
    folder = ledger_workspace.scaffold("sw", {"id": "t2", "title": "x"})
    (folder / "progress.md").write_text("one\ntwo\n\nthree\nfour\n", encoding="utf-8")
    (folder / "proof.md").write_text("run 7 green\n", encoding="utf-8")
    assert ledger_workspace.tails("sw", "t2") == {"latest_progress": "two\nthree\nfour", "latest_proof": "run 7 green"}


def test_tails_of_an_empty_or_missing_folder_are_empty():
    ledger_workspace.scaffold("sw", {"id": "t3", "title": "x"})
    assert ledger_workspace.tails("sw", "t3") == {}
    assert ledger_workspace.tails("sw", "nothing") == {}


@pytest.mark.parametrize(
    "slug, task_id", [("sw", "../x"), ("../../etc", "t1"), ("sw", "a/b"), ("", "t1"), ("sw", "t1\n")]
)
def test_a_slug_or_task_id_that_is_not_a_plain_name_is_refused(slug, task_id):
    with pytest.raises(ValueError):
        ledger_workspace.scaffold(slug, {"id": task_id, "title": "x"})
