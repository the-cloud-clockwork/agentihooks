import pytest

from scripts.swarm import prompt

pytestmark = pytest.mark.unit


def test_master_startup_requires_explicit_work_settlement_and_information_receipts():
    text = prompt.build_master("sw", "/repo", "master", {})
    assert (
        "Information closes on read or confirmed receipt. "
        "After handling a confirmed work item, explicitly reply or close it with where the work went; "
        "keep unresolved work and proof checks open."
    ) in text


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_worker_startup_marks_pure_status_reports_as_information(lane):
    task = {"id": "t1", "title": "Do it", "phase": "p1", "lane": lane}
    text = prompt.build("sw", "/repo", lane, "worker", task)
    assert (
        "Mark status reports with --fyi when they request no receiver action; information closes on receipt. "
        "After handling confirmed work, explicitly reply or close it with an outcome. "
        "Keep unresolved work and proof checks open."
    ) in text
