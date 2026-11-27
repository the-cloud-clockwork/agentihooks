"""A task trimmed to what its prompt shows renders the same prompt and trace rows as the task with every recap."""

from typing import Any

import pytest

from scripts.swarm import priming_trace, prompt

SEAT = "eng-1@sw"


def whole(count: int = 9) -> dict[str, Any]:
    """A task carrying every recap of its seat, newest first, and learned notes of each kind the prompt treats apart."""
    recaps = [{"occupant": f"sw-eng-{n}", "task": "t1", "text": f"recap {n}", "at": n} for n in range(count, 0, -1)]
    learned = [
        {"occupant": "sw-eng-1", "text": "canon note", "at": 1, "maturity": "canon", "seat": SEAT, "number": 1},
        {"occupant": "sw-eng-2", "text": "held note", "at": 2, "maturity": "note", "seat": SEAT, "number": 2},
        {"occupant": "sw-eng-3", "text": "data note", "at": 3, "maturity": "data", "seat": SEAT, "number": 3},
        {"occupant": "sw-eng-4", "text": "plain note", "at": 4, "maturity": "note", "seat": SEAT, "number": 4},
    ]
    learned[1]["withheld"] = True
    return {"id": "t1", "seat": SEAT, "culture": "one culture line", "recaps": recaps, "learned": learned}


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_carried_task_renders_the_same_prompt_and_trace_as_the_whole_task() -> None:
    task = whole()

    carried = prompt.carried(task)

    assert [recap["text"] for recap in carried["recaps"]] == ["recap 9", "recap 8", "recap 7", "recap 6"]
    assert carried[prompt.RECAPS_TOTAL] == 9
    assert prompt.priming_lines(carried) == prompt.priming_lines(task)
    assert priming_trace.rows("sw", carried) == priming_trace.rows("sw", task)


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_carried_task_keeps_only_the_learned_notes_its_prompt_counts() -> None:
    assert [note["text"] for note in prompt.carried(whole())["learned"]] == ["canon note", "data note", "plain note"]


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_seat_with_only_the_shown_recaps_counts_none_kept() -> None:
    lines = prompt.priming_lines(prompt.carried(whole(1 + prompt.OLDER_RECAPS)))

    assert "Older recaps, newest first:" in "\n".join(lines)
    assert not [line for line in lines if "older recaps are kept on the seat" in line]


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_a_seat_with_one_recap_past_the_shown_ones_counts_it_kept() -> None:
    lines = prompt.priming_lines(prompt.carried(whole(2 + prompt.OLDER_RECAPS)))

    assert "1 older recaps are kept on the seat and not shown." in lines
