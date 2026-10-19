import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_validation, models  # noqa: E402

MOVED = (
    "LEGACY_LINE_RE",
    "LISTS",
    "THREADS",
    "legacy_entries",
    "time_left_value",
    "normalize",
    "validate_thread",
    "validate",
)


@pytest.mark.parametrize("name", MOVED)
def test_core_serves_the_validation_names(name):
    assert getattr(core, name) is getattr(ledger_validation, name)


def test_lists_and_threads_come_from_the_model():
    assert (ledger_validation.LISTS, ledger_validation.THREADS) == (models.LISTS, models.THREADS)


def test_legacy_entries_split_comment_blobs_per_author_line():
    text = "ann 12:00Z: first\n  more\nbob (x): second\n\n"
    assert ledger_validation.legacy_entries(text, "p", "earlier", True) == [
        {"id": "p-0", "by": "ann", "at": 0, "text": "first\nmore"},
        {"id": "p-1", "by": "bob", "at": 0, "text": "second"},
    ]
    assert ledger_validation.legacy_entries("loose\nann: x", "p", "earlier", True) == [
        {"id": "p-0", "by": "earlier", "at": 0, "text": "loose"},
        {"id": "p-1", "by": "ann", "at": 0, "text": "x"},
    ]


def test_legacy_entries_keep_an_unsplit_field_whole():
    assert ledger_validation.legacy_entries("  a: b \n c ", "n", "operator", False) == [
        {"id": "n-0", "by": "operator", "at": 0, "text": "a: b \n c"}
    ]
    assert ledger_validation.legacy_entries("  ", "n", "operator", False) == []
    assert ledger_validation.legacy_entries(None, "n", "operator", True) == []


@pytest.mark.parametrize(
    ("value", "minutes"),
    (("90", 90), (" 1h 5m ", 65), ("2h", 120), ("7m", 7), (3, 3), (0, 0), ("", None), ("h", None), (-1, None)),
)
def test_time_left_value_reads_minutes(value, minutes):
    assert ledger_validation.time_left_value(value) == minutes


def test_time_left_value_refuses_booleans_and_floats():
    assert (ledger_validation.time_left_value(True), ledger_validation.time_left_value(1.0)) == (None, None)


def test_normalize_gives_every_collection_and_thread():
    doc = ledger_validation.normalize(
        {"notes": "remember", "questions": [{"id": "q1", "answer": "yes"}], "projection": 1}
    )
    assert "projection" not in doc
    assert doc["notes"] == [{"id": "legacy-notes-0", "by": "operator", "at": 0, "text": "remember"}]
    assert doc["questions"][0]["answers"] == [
        {"id": "legacy-questions-q1-answer-0", "by": "operator", "at": 0, "text": "yes"}
    ]
    assert "answer" not in doc["questions"][0]
    for key in ("chat", "priorities", "notifications", "artifacts", "artifact_trash", "tasks", "plans", "slices"):
        assert doc[key] == []
    assert (doc["freezes"], doc["time_left_minutes"]) == ([], None)


def test_normalize_leaves_a_non_object_alone():
    assert ledger_validation.normalize([1]) == [1]


def test_validate_thread_refuses_duplicate_and_missing_ids():
    with pytest.raises(ValueError) as caught:
        ledger_validation.validate_thread("chat", [{"id": "a"}, {"id": "a"}])
    assert str(caught.value) == "chat: every entry needs a unique string id"
    with pytest.raises(ValueError) as caught:
        ledger_validation.validate_thread("chat", {"id": "a"})
    assert str(caught.value) == "chat must be a list of entries"
    with pytest.raises(ValueError) as caught:
        ledger_validation.validate_thread("chat", [{"id": "a", "text": 1}])
    assert str(caught.value) == "chat/a: text and by must be strings"


def test_validate_refuses_a_bad_time_left_and_a_non_object():
    with pytest.raises(ValueError) as caught:
        ledger_validation.validate({"time_left_minutes": "5"})
    assert str(caught.value) == "time_left_minutes must be a nonnegative integer"
    with pytest.raises(ValueError) as caught:
        ledger_validation.validate([])
    assert str(caught.value) == "seed is not a JSON object"
    ledger_validation.validate({"time_left_minutes": 5})
