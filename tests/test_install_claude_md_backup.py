from datetime import datetime, tzinfo
from pathlib import Path
from typing import Self

import pytest

from scripts import install


class _PinnedClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)


def test_same_second_backup_takes_the_next_free_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "STATE_JSON", tmp_path / "state.json")
    monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", tmp_path)
    monkeypatch.setattr(install, "datetime", _PinnedClock)
    dst = tmp_path / "CLAUDE.md"
    dst.write_text("current\n")
    taken = [tmp_path / "CLAUDE.md.bak.20260102_030405", tmp_path / "CLAUDE.md.bak.20260102_030405_1"]
    for index, path in enumerate(taken):
        path.write_text(f"earlier {index}\n")

    backup = install._backup_existing_claude_md(dst)

    fresh = tmp_path / "CLAUDE.md.bak.20260102_030405_2"
    assert backup == fresh
    assert fresh.read_text() == "current\n"
    assert [path.read_text() for path in taken] == ["earlier 0\n", "earlier 1\n"]
