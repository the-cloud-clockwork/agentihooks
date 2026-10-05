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


def test_the_master_hands_the_operator_the_ledger_link_first_and_on_request():
    master = prompt.build_master("sw", "/repo", "sw-master-1", {})
    assert "agentihooks swarm sw url" in master
    assert "first message" in master and "what is my ledger link" in master
    assert "http://127.0.0.1:8765/sw" not in master
