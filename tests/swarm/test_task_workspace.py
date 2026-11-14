import pytest

from scripts.swarm import prompt
from scripts.swarm_ledger import ledger_workspace, plan_read


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
