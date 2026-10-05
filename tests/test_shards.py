import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests.shards import assign_files, discover_test_files, slowest_first, source_sizes, warm_imports

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def test_every_test_file_lands_in_exactly_one_shard():
    files = discover_test_files(_ROOT)
    groups = assign_files({}, files, 4, {})
    assigned = [path for group in groups for path in group]
    assert sorted(assigned) == files
    assert "tests/test_shards.py" in files
    assert "tests/lifecycle/test_lease.py" in files


def test_shards_balance_the_stored_durations_per_file():
    durations = {
        "tests/test_a.py::t1": 4.0,
        "tests/test_a.py::t2": 2.0,
        "tests/test_b.py::t": 5.0,
        "tests/test_c.py::t": 3.0,
        "tests/test_d.py::t": 3.0,
        "tests/gone.py::t": 9.0,
    }
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py", "tests/test_e.py"]
    groups = assign_files(durations, files, 2, {})
    assert sorted(map(sorted, groups)) == [
        ["tests/test_a.py", "tests/test_d.py"],
        ["tests/test_b.py", "tests/test_c.py", "tests/test_e.py"],
    ]


def test_shards_weigh_source_size_since_every_worker_collects_the_whole_shard():
    durations = {"tests/test_a.py::t": 1.0, "tests/test_b.py::t": 1.0, "tests/test_c.py::t": 1.0}
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    sizes = {"tests/test_a.py": 1_000_000, "tests/test_b.py": 1_000, "tests/test_c.py": 1_000}
    groups = assign_files(durations, files, 2, sizes)
    assert sorted(map(sorted, groups)) == [["tests/test_a.py"], ["tests/test_b.py", "tests/test_c.py"]]


def test_source_sizes_are_the_file_sizes_in_bytes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x" * 7)
    (tmp_path / "tests" / "test_b.py").write_text("")
    assert source_sizes(tmp_path, ["tests/test_a.py", "tests/test_b.py"]) == {
        "tests/test_a.py": 7,
        "tests/test_b.py": 0,
    }


def test_shard_option_weighs_source_size(tmp_path):
    (tmp_path / "tests").mkdir()
    for name, size in (("test_a.py", 1_000_000), ("test_b.py", 10), ("test_c.py", 10)):
        (tmp_path / "tests" / name).write_text("x" * size)
    (tmp_path / ".test_durations").write_text(json.dumps({f"tests/test_{n}.py::t": 1.0 for n in "abc"}))
    config = SimpleNamespace(getoption=lambda name: "1/2", stash=pytest.Stash(), rootpath=tmp_path)
    ignored = {n for n in "abc" if conftest.pytest_ignore_collect(tmp_path / "tests" / f"test_{n}.py", config)}
    assert ignored == {"b", "c"}


def test_shard_option_collects_only_that_shards_files(pytestconfig):
    files = discover_test_files(_ROOT)
    shard = 2
    expected = set(assign_files(_stored_durations(), files, 4, source_sizes(_ROOT, files))[shard - 1])
    assert "shard" in vars(pytestconfig.option)
    config = SimpleNamespace(getoption=lambda name: f"{shard}/4", stash=pytest.Stash(), rootpath=_ROOT)
    kept = {path for path in files if not conftest.pytest_ignore_collect(_ROOT / path, config)}
    assert kept == expected
    assert conftest.pytest_ignore_collect(_ROOT / "tests", config) is None
    assert conftest.pytest_ignore_collect(_ROOT / "tests" / "conftest.py", config) is None


def test_slow_tests_run_first_and_the_rest_keep_their_order():
    durations = {"t::a": 0.01, "t::b": 0.5, "t::c": 0.02, "t::d[x]": 1.5, "t::e": 0.1}
    nodeids = ["t::a", "t::b", "t::c", "t::d[x]@probe", "t::e", "t::new"]
    assert slowest_first(nodeids, durations, 0.1) == ["t::d[x]@probe", "t::b", "t::e", "t::a", "t::c", "t::new"]


def _modified_order(tmp_path, nodeids, **config):
    (tmp_path / ".test_durations").write_text(json.dumps({"t::fast": 0.001, "t::slow": 2.0}))
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in nodeids]
    conftest.pytest_collection_modifyitems(SimpleNamespace(stash=pytest.Stash(), rootpath=tmp_path, **config), items)
    return [item.nodeid for item in items]


def test_xdist_workers_hand_out_the_slow_tests_first(tmp_path):
    assert _modified_order(tmp_path, ["t::fast", "t::slow"], workerinput={}) == ["t::slow", "t::fast"]


def test_a_run_without_workers_keeps_file_order(tmp_path):
    assert _modified_order(tmp_path, ["t::fast", "t::slow"]) == ["t::fast", "t::slow"]


def _stored_durations():
    return json.loads((_ROOT / ".test_durations").read_text())


def _warm_package(tmp_path, monkeypatch, files):
    package = tmp_path / "warmpkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for name, body in files.items():
        (package / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    return package


def test_warm_imports_leave_the_rewritten_bytecode_for_the_workers(tmp_path, monkeypatch):
    package = _warm_package(tmp_path, monkeypatch, {"test_one": "assert 1\n", "test_two": "assert 2\n"})
    statuses = [os.waitpid(pid, 0)[1] for pid in warm_imports(["warmpkg.test_one", "warmpkg.test_two"], 2)]
    assert statuses == [0, 0]
    assert "warmpkg.test_one" not in sys.modules
    for name in ("test_one", "test_two"):
        assert list((package / "__pycache__").glob(f"{name}.*-pytest-*.pyc"))


def test_a_module_that_fails_to_import_leaves_the_rest_of_its_stride_warmed(tmp_path, monkeypatch):
    package = _warm_package(tmp_path, monkeypatch, {"test_bad": "raise SystemExit(3)\n", "test_ok": "assert 1\n"})
    statuses = [os.waitpid(pid, 0)[1] for pid in warm_imports(["warmpkg.test_bad", "warmpkg.test_ok"], 1)]
    assert statuses == [0]
    assert list((package / "__pycache__").glob("test_ok.*-pytest-*.pyc"))


def _configured(monkeypatch, numprocesses, **extra):
    calls = []
    monkeypatch.setattr(conftest, "warm_imports", lambda modules, workers: calls.append((modules, workers)) or [7])
    config = SimpleNamespace(
        option=SimpleNamespace(numprocesses=numprocesses),
        getoption=lambda name: "2/4",
        stash=pytest.Stash(),
        rootpath=_ROOT,
        **extra,
    )
    conftest.pytest_configure(config)
    return calls, config


def test_the_controller_warms_its_shards_test_modules_once_per_worker(monkeypatch):
    calls, config = _configured(monkeypatch, 4)
    files = discover_test_files(_ROOT)
    shard = sorted(assign_files(_stored_durations(), files, 4, source_sizes(_ROOT, files))[1])
    assert calls == [([path.removesuffix(".py").replace("/", ".") for path in shard], 4)]
    reaped = []
    monkeypatch.setattr(conftest.os, "waitpid", lambda pid, flags: reaped.append(pid))
    conftest.pytest_unconfigure(config)
    assert reaped == [7]


def test_workers_and_runs_without_workers_warm_nothing(monkeypatch):
    assert _configured(monkeypatch, 4, workerinput={})[0] == []
    assert _configured(monkeypatch, None)[0] == []
