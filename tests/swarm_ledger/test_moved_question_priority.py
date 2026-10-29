import pytest

from hooks.context import master_question
from scripts.swarm_ledger.ledger_priorities import wanted

pytestmark = pytest.mark.unit


def test_a_question_moved_off_the_master_pane_shows_in_the_operator_priorities():
    text = master_question._text({"question": "Freeze the hive phase?", "options": [{"label": "Yes"}, {"label": "No"}]})
    rows = wanted({"phases": [], "questions": [{"id": "q1", "text": text}]})
    assert rows == {"questions/q1": "Answer: Freeze the hive phase? Options: Yes; No."}
