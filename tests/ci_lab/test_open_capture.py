import functools
import io
import os
import subprocess
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


def _from(filenames: tuple[str, ...], call: Callable[[], Any]) -> Any:
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
    tree.mkdir()
    monkeypatch.chdir(tree)
    log = io.BytesIO()
    capture = open_capture.Capture(str(tree), log)
    frame = sys._getframe()
    events = [
        ("relative.txt", "r", 0),
        (str(tree / "a.txt"), "r", 0),
        (str(tree / "a.txt"), "rb", 0),
        (tree / "b.txt", "rb", 0),
        (os.fsencode(tree / "c.txt"), "r", 0),
        (os.fsencode(tree) + b"/\xff.txt", "r", 0),
        (str(tree / "d.txt"), None, 0),
        (str(tree / "w.txt"), "w", 0),
        (str(tree / "x.txt"), "x", 0),
        (str(tree / "y.txt"), "ab", 0),
        (3, "r", 0),
        (str(tmp_path / "tree2" / "e.txt"), "r", 0),
        (str(tmp_path / "f.txt"), "r", 0),
    ]

    for args in events:
        capture.audit(args, frame)

    assert log.getvalue() == b"relative.txt\na.txt\nb.txt\nc.txt\n\xff.txt\nd.txt\n"


def test_a_read_by_the_import_system_or_outside_a_worker_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = io.BytesIO()
    capture = open_capture.Capture(str(tmp_path), log)

    def read() -> bool:
        capture.audit((str(tmp_path / "b.txt"), "r", 0), sys._getframe(1))
        return True

    monkeypatch.delenv(open_capture.WORKER, raising=False)
    capture.audit((str(tmp_path / "a.txt"), "r", 0), sys._getframe())
    monkeypatch.setenv(open_capture.WORKER, "gw1")
    _from((IMPORT,), read)
    assert log.getvalue() == b""
    _from((TEST,), read)

    assert log.getvalue() == b"b.txt\n"


def test_install_hooks_a_capture_that_writes_this_process_log_in_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks: list[Callable[[str, tuple[Any, ...]], None]] = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    monkeypatch.setenv(open_capture.TREE, str(tmp_path / "tree"))
    monkeypatch.setenv(open_capture.OPENED, str(tmp_path))
    monkeypatch.setenv(open_capture.WORKER, "gw0")

    open_capture.install()
    _from((IMPORT,), functools.partial(hooks[0], "open", (str(tmp_path / "tree" / "module.json"), "r", 0)))
    hooks[0]("exec", (str(tmp_path / "tree" / "code.json"), "r", 0))
    hooks[0]("open", (str(tmp_path / "tree" / "data.json"), "r", 0))

    assert len(hooks) == 1
    assert (tmp_path / f"{os.getpid()}.txt").read_bytes() == b"data.json\n"


def test_an_installed_capture_survives_the_audit_event_its_own_frame_lookup_raises(tmp_path: Path) -> None:
    tree, folder = tmp_path / "tree", tmp_path / "opened"
    for path in (tree, folder):
        path.mkdir()
    (tree / "data.txt").write_text("data")
    load = "spec = importlib.util.spec_from_file_location('capture', sys.argv[1])"
    code = f"import importlib.util, sys; {load}; m = importlib.util.module_from_spec(spec); "
    code += "spec.loader.exec_module(m); m.install(); open('data.txt').read()"
    env = {
        "PATH": os.defpath,
        open_capture.TREE: str(tree),
        open_capture.OPENED: str(folder),
        open_capture.WORKER: "gw0",
    }

    done = subprocess.run(
        [sys.executable, "-c", code, open_capture.__file__], cwd=tree, env=env, capture_output=True, text=True
    )

    assert (done.returncode, done.stderr) == (0, "")
    assert [log.read_text() for log in folder.iterdir()] == ["data.txt\n"]


def test_a_relative_read_after_the_working_folder_is_gone_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(open_capture.WORKER, "gw0")
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    log = io.BytesIO()

    open_capture.Capture(str(tmp_path), log).audit(("data.txt", "r", 0), sys._getframe())

    assert log.getvalue() == b""


@pytest.mark.parametrize("missing", ["CI_LAB_TREE", "CI_LAB_OPENED"])
def test_install_hooks_nothing_unless_both_the_tree_and_the_folder_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    hooks: list[Callable[[str, tuple[Any, ...]], None]] = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    folder = tmp_path / "opened"
    folder.mkdir()
    monkeypatch.setenv(open_capture.TREE, str(tmp_path))
    monkeypatch.setenv(open_capture.OPENED, str(folder))
    monkeypatch.setenv(missing, "")

    open_capture.install()

    assert hooks == []
    assert list(folder.iterdir()) == []
