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


def test_the_master_hands_the_operator_the_ledger_link_only_on_request():
    master = prompt.build_master("sw", "/repo", "sw-master-1", {})
    assert "When he asks for his ledger link, answer with the line agentihooks swarm sw --as sw-master-1 url" in master
    assert "first message" not in master
    assert "http://127.0.0.1:8765/sw" not in master


def test_the_master_prompt_says_an_in_scope_comment_keeps_a_piece_in_the_plan_trace():
    master = prompt.build_master("sw", "/repo", "sw-master-1", {})
    assert '"In scope: <the piece>": the plan trace keeps a piece that comment covers.' in master


def test_a_seat_two_master_prompt_says_which_seat_it_holds_and_the_lead_prompt_does_not():
    seat_task = {"id": "master-2", "seat": "master-2@sw", "masters": 2, "owns": ["p2"]}
    assert "You hold master seat master-2@sw, one of 2 masters of swarm sw." in prompt.build_master(
        "sw", "/repo", "m2", seat_task
    )
    assert "You hold master seat" not in prompt.build_master("sw", "/repo", "m1", {"seat": "master@sw"})
