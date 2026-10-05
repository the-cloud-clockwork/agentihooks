import pytest

from scripts.swarm import prompt

TASK = {"id": "t1", "title": "Do it", "phase": "p1", "lane": "eng"}
RULE = "Page chat is for the master: act on a chat line only when it starts with @ and your name."


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_engineer_and_ci_prompts_leave_unaddressed_page_chat_to_the_master(lane):
    assert RULE in prompt.build("sw", "/repo", lane, f"sw-{lane}-1", {**TASK, "lane": lane})


def test_the_master_prompt_keeps_answering_every_chat_line():
    master = prompt.build_master("sw", "/repo", "sw-master-1", {})
    assert RULE not in master and "Answer every operator chat message" in master
