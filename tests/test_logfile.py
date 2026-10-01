import pytest

from hooks.logfile import append_text, rotate_if_full


@pytest.fixture
def logs(tmp_path):
    path = tmp_path / "logs"
    path.mkdir()
    return path


def test_append_creates_parent_and_appends(logs):
    path = logs / "sub" / "hooks.log"
    append_text(path, "a\n")
    append_text(path, "b\n")
    assert path.read_text() == "a\nb\n"


def test_rotate_below_limit_keeps_file(logs):
    path = logs / "hooks.log"
    path.write_text("x" * 10)
    rotate_if_full(path, limit=11, backups=3)
    assert path.read_text() == "x" * 10
    assert not (logs / "hooks.log.1").exists()


def test_rotate_shifts_backups_and_drops_oldest(logs):
    path = logs / "hooks.log"
    for generation in "abcd":
        path.write_text(generation * 10)
        rotate_if_full(path, limit=10, backups=2)
    assert not path.exists()
    assert (logs / "hooks.log.1").read_text() == "d" * 10
    assert (logs / "hooks.log.2").read_text() == "c" * 10
    assert not (logs / "hooks.log.3").exists()


def test_rotate_zero_backups_truncates(logs):
    path = logs / "hooks.log"
    path.write_text("x" * 10)
    rotate_if_full(path, limit=5, backups=0)
    assert list(logs.iterdir()) == []


def test_missing_file_is_noop(logs):
    rotate_if_full(logs / "absent.log", limit=1, backups=1)
    assert list(logs.iterdir()) == []


def test_hook_log_stays_bounded_under_sustained_writes(logs, monkeypatch):
    import hooks.common as common

    path = logs / "hooks.log"
    monkeypatch.setattr(common, "LOG_ENABLED", True)
    monkeypatch.setattr(common, "LOG_FILE", str(path))
    monkeypatch.setattr("hooks.logfile.rotate_if_full.__defaults__", (4096, 2))
    for index in range(2000):
        common.log(f"event-{index}", {"pad": "y" * 40})
    assert sorted(f.name for f in logs.iterdir()) == ["hooks.log", "hooks.log.1", "hooks.log.2"]
    assert all(f.stat().st_size < 4096 + 200 for f in logs.iterdir())
    assert "event-1999" in path.read_text()
