import io
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import open_capture

IMPORT = "<frozen importlib._bootstrap>"
PATHLIB = f"{os.sep}lib{os.sep}pathlib.py"
TOKENIZE = f"{os.sep}lib{os.sep}tokenize.py"
TEST = f"{os.sep}repo{os.sep}tests{os.sep}test_x.py"


def _from(filenames: tuple[str, ...], call: Callable[[], bool]) -> bool:
    """Call call from a stack of frames whose code sits in each filename, the first outermost."""
    for filename in reversed(filenames):
        namespace: dict[str, Any] = {"call": call}
        exec(compile("def frame():\n    return call()\n", filename, "exec"), namespace)
        call = namespace["frame"]
    return call()


def _here() -> bool:
    return open_capture.loading(sys._getframe(1))


def test_loading_is_an_import_assertion_rewrite_or_source_lookup_past_pathlib_helpers() -> None:
    assert _from((IMPORT,), _here)
    assert _from((f"{os.sep}usr{os.sep}lib{os.sep}importlib{os.sep}util.py",), _here)
    assert _from((f"{os.sep}site{os.sep}_pytest{os.sep}assertion{os.sep}rewrite.py",), _here)
    assert _from((f"{os.sep}usr{os.sep}lib{os.sep}linecache.py",), _here)
    assert _from((IMPORT, PATHLIB), _here)
    assert _from((IMPORT, TOKENIZE, PATHLIB), _here)
    assert not _from((TEST,), _here)
    assert not _from((TEST, PATHLIB), _here)
    assert not _from((PATHLIB,), lambda: open_capture.loading(None))


def test_a_worker_records_each_read_under_the_tree_once_relative_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(open_capture.WORKER, "gw0")
    tree = tmp_path / "tree"
    log = io.StringIO()
    capture = open_capture.Capture(str(tree), log)
    frame = sys._getframe()
    events = [
        ("open", (str(tree / "a.txt"), "r", 0)),
        ("open", (str(tree / "a.txt"), "rb", 0)),
        ("open", (tree / "b.txt", "rb", 0)),
        ("open", (os.fsencode(tree / "c.txt"), "r", 0)),
        ("open", (str(tree / "d.txt"), None, 0)),
        ("open", (str(tree / "w.txt"), "w", 0)),
        ("open", (str(tree / "x.txt"), "x", 0)),
        ("open", (str(tree / "y.txt"), "ab", 0)),
        ("open", (3, "r", 0)),
        ("open", (str(tmp_path / "tree2" / "e.txt"), "r", 0)),
        ("open", (str(tmp_path / "f.txt"), "r", 0)),
        ("exec", (str(tree / "g.txt"), "r", 0)),
    ]

    for event, args in events:
        capture.audit(event, args, frame)

    assert log.getvalue() == "a.txt\nb.txt\nc.txt\nd.txt\n"


def test_a_read_by_the_import_system_or_outside_a_worker_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = io.StringIO()
    capture = open_capture.Capture(str(tmp_path), log)

    def read() -> bool:
        capture.audit("open", (str(tmp_path / "b.txt"), "r", 0), sys._getframe(1))
        return True

    monkeypatch.delenv(open_capture.WORKER, raising=False)
    capture.audit("open", (str(tmp_path / "a.txt"), "r", 0), sys._getframe())
    monkeypatch.setenv(open_capture.WORKER, "gw1")
    _from((IMPORT,), read)
    assert log.getvalue() == ""
    _from((TEST,), read)

    assert log.getvalue() == "b.txt\n"


def test_install_hooks_a_capture_that_writes_this_process_log_in_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks: list[Callable[[str, tuple[Any, ...]], None]] = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    monkeypatch.setenv(open_capture.TREE, str(tmp_path / "tree"))
    monkeypatch.setenv(open_capture.OPENED, str(tmp_path))
    monkeypatch.setenv(open_capture.WORKER, "gw0")

    open_capture.install()
    hooks[0]("open", (str(tmp_path / "tree" / "data.json"), "r", 0))

    assert len(hooks) == 1
    assert (tmp_path / f"{os.getpid()}.txt").read_text() == "data.json\n"


@pytest.mark.parametrize("missing", ["CI_LAB_TREE", "CI_LAB_OPENED"])
def test_install_hooks_nothing_unless_both_the_tree_and_the_folder_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    hooks: list[Callable[[str, tuple[Any, ...]], None]] = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    monkeypatch.setenv(open_capture.TREE, str(tmp_path))
    monkeypatch.setenv(open_capture.OPENED, str(tmp_path))
    monkeypatch.setenv(missing, "")

    open_capture.install()

    assert hooks == []
    assert list(tmp_path.iterdir()) == []
