from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "ci-timing.md"


def test_ci_timing_doc_has_table_with_run_ids():
    text = DOC.read_text()
    assert "| Job | Step |" in text
    assert "Run tests" in text
    assert "35156774129" in text
