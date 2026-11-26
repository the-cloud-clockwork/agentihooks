"""The quick seat files a seat reader opens: one folder per seat with its handoff and learned notes, one culture file."""

from pathlib import Path

import pytest

from scripts.quick import paths

pytestmark = pytest.mark.unit


@pytest.mark.feature("seat-brief")
def test_a_quick_seat_keeps_its_handoff_and_learned_notes_in_its_seat_folder(tmp_path: Path) -> None:
    assert paths.handoff(tmp_path, "eng-1") == tmp_path / "seats" / "eng-1" / "handoff.md"
    assert paths.learned(tmp_path, "eng-1") == tmp_path / "seats" / "eng-1" / "learned.md"
    assert paths.culture(tmp_path) == tmp_path / "culture.md"


@pytest.mark.feature("seat-brief")
def test_creating_a_quick_folder_leaves_every_seat_file_a_reader_opens(tmp_path: Path) -> None:
    paths.create(tmp_path, ["master", "eng-1"])

    for seat in ("master", "eng-1"):
        assert paths.handoff(tmp_path, seat).is_file()
        assert paths.learned(tmp_path, seat).is_file()
    assert paths.culture(tmp_path).is_file()
    assert paths.read_text(tmp_path / "missing.md") == ""
