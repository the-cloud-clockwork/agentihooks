import errno
import os
import shutil

import pytest

from tests.conftest import memory_folder


def late_writes(monkeypatch, times):
    real, planted = os.rmdir, []

    def rmdir(path, *, dir_fd=None):
        if os.path.basename(path) == "state" and len(planted) < times:
            planted.append(path)
            os.close(os.open(os.path.join(path, f"late-{len(planted)}"), os.O_CREAT | os.O_WRONLY, dir_fd=dir_fd))
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", rmdir)
    return planted


def test_a_late_write_into_the_state_folder_still_leaves_the_folder_removed(monkeypatch):
    with memory_folder() as path:
        (path / ".local" / "state" / "herdr").mkdir(parents=True)
        planted = late_writes(monkeypatch, 1)
    assert planted
    assert not path.exists()


def test_writes_that_never_stop_fail_the_teardown_after_the_settle_window(monkeypatch):
    with pytest.raises(OSError) as failed:
        with memory_folder(settle_s=0.2) as path:
            (path / ".local" / "state").mkdir(parents=True)
            planted = late_writes(monkeypatch, 10**6)
    monkeypatch.undo()
    assert failed.value.errno == errno.ENOTEMPTY
    assert len(planted) > 1
    shutil.rmtree(path)
