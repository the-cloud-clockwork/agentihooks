import json

from scripts.swarm import prompt


def test_master_reads_the_previous_summary_before_its_first_chat_line(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    summary = "Summary\nWe stopped with the parser finished.\nTasks still open:\n- none"
    (tmp_path / "sw.json").write_text(json.dumps({"overview": f"Intent.\n\n{summary}"}))
    text = prompt.build_master("sw", "/repo", "sw-master-2", {"seat": "master@sw", "culture": "Keep records"})
    assert summary in text
    assert "first ledger chat line" in text
    assert (
        "After posting your summary acknowledgement in ledger chat, send the ledger page link as your second chat line"
        in text
    )
    assert "Your next message after joining" not in text
    assert "summary" in text.lower()
    assert text.index(summary) < text.index("Your standing duties:")
    assert "Keep records" in text


def test_master_without_a_summary_keeps_its_existing_priming(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    text = prompt.build_master("sw", "/repo", "sw-master-1", {})
    assert "Your standing duties:" in text
    assert "first ledger chat line" not in text
