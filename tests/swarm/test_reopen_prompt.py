import pytest

from scripts.swarm import prompt
from tests.swarm_ledger import legacy_page

FIRST_CONTACT = (
    "first ledger chat line",
    "summary acknowledgement",
    "send the ledger page link",
    "Your first message after joining",
    "say --to operator",
    "priority add",
    "question add",
    "--needs-operator",
)


def test_master_keeps_the_previous_summary_for_when_the_operator_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    summary = "Summary\nWe stopped with the parser finished.\nTasks still open:\n- none"
    legacy_page.store(tmp_path, "sw", {"overview": f"Intent.\n\n{summary}"})
    text = prompt.build_master("sw", "/repo", "sw-master-2", {"seat": "master@sw", "culture": "Keep records"})
    assert summary in text
    assert "When the operator writes, use this summary to say where the work stopped in plain words." in text
    assert text.index(summary) < text.index("Your standing duties:")
    assert "Keep records" in text


@pytest.mark.parametrize("summary", ["", "Summary\nWe stopped with the parser finished."])
def test_the_master_opening_prompt_orders_no_first_contact(monkeypatch, tmp_path, summary):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    legacy_page.store(tmp_path, "sw", {"overview": f"Intent.\n\n{summary}"})
    text = prompt.build_master("sw", "/repo", "sw-master-1", {"seat": "master@sw"})
    assert [phrase for phrase in FIRST_CONTACT if phrase in text] == []
    assert prompt.FIRST_CONTACT in text
    assert "closes the message. Answer what the operator types in this pane here." in text
    assert (
        "When he asks for his ledger link, answer with the line agentihooks swarm sw --as sw-master-1 url prints."
        in text
    )
