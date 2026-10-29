import json

import pytest

from scripts.swarm_v2.worker import completion

EXECUTION = "exe-" + "a" * 32
REPORT = {"schema_version": 1, "execution_id": EXECUTION, "generation": 3, "transcript_end": 9}


@pytest.mark.parametrize("end", [0, 9])
def test_completion_reports_the_final_record_count(tmp_path, monkeypatch, end):
    path = tmp_path / "termination.log"
    path.touch()
    monkeypatch.setattr(completion, "TERMINATION_LOG", path)
    completion.report(
        {"authority": {"execution_id": EXECUTION, "generation": 3}},
        {"status": "complete", "transcript_end": end},
    )
    expected = {**REPORT, "transcript_end": end}
    assert path.read_bytes() == json.dumps(expected).encode("utf-8")
    assert completion.transcript_end(path.read_text(), EXECUTION, 3) == end


def test_an_incomplete_export_does_not_report_a_final_boundary(tmp_path, monkeypatch):
    path = tmp_path / "termination.log"
    path.write_text("kept")
    monkeypatch.setattr(completion, "TERMINATION_LOG", path)
    completion.report({}, {"status": "incomplete"})
    assert path.read_text() == "kept"


def test_a_local_export_does_not_create_a_kubernetes_termination_file(tmp_path, monkeypatch):
    path = tmp_path / "absent"
    monkeypatch.setattr(completion, "TERMINATION_LOG", path)
    completion.report({}, {"status": "complete", "transcript_end": 9})
    assert not path.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"execution_id": "another"},
        {"generation": 4},
        {"generation": True},
        {"generation": "3"},
        {"transcript_end": -1},
        {"transcript_end": True},
        {"transcript_end": "9"},
        {"transcript_end": None},
    ],
)
def test_a_mismatched_or_invalid_completion_report_is_refused(change):
    assert completion.transcript_end(json.dumps({**REPORT, **change}), EXECUTION, 3) is None


@pytest.mark.parametrize("message", ["", "broken", "{}", "null", "[]"])
def test_missing_or_malformed_completion_reports_are_refused(message):
    assert completion.transcript_end(message, EXECUTION, 3) is None
