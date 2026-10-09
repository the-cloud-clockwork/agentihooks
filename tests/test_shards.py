import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from xdist.workermanage import NodeManager

from hooks.secrets import scan
from tests import conftest
from tests.shards import (
    FIRST_SHARD_FILES,
    assign_files,
    discover_test_files,
    setup_nodes_in_parallel,
    slowest_first,
    source_sizes,
    warm_imports,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
_URL_CREDENTIAL = re.compile(r"://[^/\s@:]+:[^/\s@]+@")


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


def _committed(*args: str) -> str:
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=_ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def test_committed_reads_from_repository_root_in_nested_copy(monkeypatch):
    monkeypatch.setattr(__name__ + "._ROOT", _ROOT / "tests")
    assert _committed("ls-files", ".test_durations").split() == [".test_durations"]


def _credential_shaped(nodeids):
    return [nodeid for nodeid in nodeids if scan(nodeid, mode="strict") or _URL_CREDENTIAL.search(nodeid)]


def test_the_credential_check_flags_token_and_url_password_names():
    planted = [
        "t.py::t[" + "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8]",
        "t.py::t[https://user:fixture-" + "password@github.com/o/r.git]",
    ]
    assert _credential_shaped([*planted, "t.py::t[github]", "t.py::t[https-userinfo]"]) == planted


def test_committed_durations_name_no_credential_shaped_case():
    stored = _committed("ls-files", ".test_durations*").split()
    assert stored
    flagged = [
        f"{path}: {nodeid}"
        for path in stored
        for nodeid in _credential_shaped(json.loads(_committed("show", f"HEAD:{path}")))
    ]
    assert flagged == []


def test_serial_group_costs_are_not_divided_across_workers():
    durations = {f"tests/test_{name}.py::t": seconds for name, seconds in zip("abcd", (5, 9, 5, 8))}
    files = [f"tests/test_{name}.py" for name in "abcd"]
    grouped = {files[0], files[2]}
    assert assign_files(durations, files, 2, {}, grouped, 4) == [[files[0], files[1]], [files[2], files[3]]]
    assert assign_files(durations, files, 2, {}, grouped, 1) == assign_files(durations, files, 2, {})


def test_shards_balance_serial_and_parallel_worker_loads():
    files = [f"tests/test_{name}.py" for name in "abcde"]
    durations = {f"{path}::test_x": seconds for path, seconds in zip(files, (40, 30, 60, 60, 60))}
    grouped = {files[0], files[1]}
    assert assign_files(durations, files, 2, {}, grouped, 4) == [
        [files[0], files[4]],
        [files[1], files[2], files[3]],
    ]


def test_measured_cases_from_one_module_balance_across_shards():
    from tests.shards import assign_nodes

    nodes = [f"tests/test_hot.py::test_{n}" for n in range(8)]
    durations = dict(zip(nodes, range(8, 0, -1)))
    parts = assign_nodes(durations, 4, {"tests/test_hot.py"}, 4)
    assert [sum(durations[node] for node in part) for part in parts] == [9, 9, 9, 9]
    assert sorted(node for part in parts for node in part) == nodes


def test_independent_worker_groups_balance_without_serializing_each_other():
    from tests.shards import assign_nodes

    nodes = [f"tests/test_{n}.py::test_x" for n in range(4)]
    durations = dict(zip(nodes, (30, 30, 20, 20)))
    grouped = {f"tests/test_{n}.py": "redis" if n % 2 == 0 else "sdk" for n in range(4)}
    parts = assign_nodes(durations, 2, grouped, 4)
    loads = [
        max(
            sum(durations[node] for node in part if grouped[node.split("::")[0]] == group) for group in ("redis", "sdk")
        )
        for part in parts
    ]
    assert loads == [30, 30]
    assert sorted(node for part in parts for node in part) == nodes


def test_worker_shards_select_each_measured_and_unknown_case_once(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_hot.py").write_text('import pytest\npytestmark = pytest.mark.xdist_group("redis")\n')
    nodes = [f"tests/test_hot.py::test_{n}" for n in range(8)]
    durations = dict(zip(nodes, range(8, 0, -1)))
    (tmp_path / ".test_durations").write_text(json.dumps(durations))
    all_nodes = nodes + ["tests/test_hot.py::test_new[a@b]"]
    selections = []
    for shard in (1, 2):
        config = SimpleNamespace(
            getoption=lambda name, shard=shard: f"{shard}/2",
            stash=pytest.Stash(),
            rootpath=tmp_path,
            option=SimpleNamespace(numprocesses=None),
            workerinput={"workercount": 4},
        )
        assert conftest.pytest_ignore_collect(tmp_path / "tests/test_hot.py", config) is None
        items = [SimpleNamespace(nodeid=node + "@redis") for node in all_nodes]
        conftest.pytest_collection_modifyitems(config, items)
        selections.append({item.nodeid.removesuffix("@redis") for item in items})
    assert selections[0].isdisjoint(selections[1])
    assert selections[0] | selections[1] == set(all_nodes)
    assert all(selections)


def test_new_case_in_a_sparsely_measured_module_stays_on_a_collecting_shard(tmp_path):
    import zlib

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sparse.py").write_text("pass\n")
    known = "tests/test_sparse.py::test_known"
    unknown = next(
        f"tests/test_sparse.py::test_new_{n}"
        for n in range(100)
        if zlib.crc32(f"tests/test_sparse.py::test_new_{n}".encode()) % 2 == 1
    )
    (tmp_path / ".test_durations").write_text(json.dumps({known: 1.0}))
    selections = []
    for shard in (1, 2):
        config = SimpleNamespace(
            getoption=lambda name, shard=shard: f"{shard}/2",
            stash=pytest.Stash(),
            rootpath=tmp_path,
            option=SimpleNamespace(numprocesses=None),
            workerinput={"workercount": 2},
        )
        items = (
            []
            if conftest.pytest_ignore_collect(tmp_path / "tests/test_sparse.py", config)
            else [SimpleNamespace(nodeid=node) for node in (known, unknown)]
        )
        conftest.pytest_collection_modifyitems(config, items)
        selections.append({item.nodeid for item in items})
    assert selections == [{known, unknown}, set()]


@pytest.mark.parametrize("workers", [None, 4])
def test_the_codex_file_runs_whole_in_the_first_shard_that_installs_the_codex_cli(tmp_path, workers):
    (tmp_path / "tests/routing").mkdir(parents=True)
    pinned = "tests/routing/test_codex_api.py"
    for path in (pinned, "tests/test_a.py", "tests/test_b.py"):
        (tmp_path / path).write_text("pass\n")
    codex = [f"{pinned}::test_{n}" for n in range(4)]
    durations = {**dict.fromkeys(codex, 2.0), "tests/test_a.py::t": 50.0, "tests/test_b.py::t": 1.0}
    (tmp_path / ".test_durations").write_text(json.dumps(durations))
    selections = []
    for shard in (1, 2, 3):
        config = SimpleNamespace(
            getoption=lambda name, shard=shard: f"{shard}/3",
            stash=pytest.Stash(),
            rootpath=tmp_path,
            option=SimpleNamespace(numprocesses=None),
            **({"workerinput": {"workercount": workers}} if workers else {}),
        )
        collected = not conftest.pytest_ignore_collect(tmp_path / pinned, config)
        items = [SimpleNamespace(nodeid=node) for node in codex] if collected else []
        conftest.pytest_collection_modifyitems(config, items)
        selections.append({item.nodeid for item in items})
    assert selections == [set(codex), set(), set()]


def test_every_file_pinned_to_the_first_shard_exists():
    assert FIRST_SHARD_FILES
    assert FIRST_SHARD_FILES <= set(discover_test_files(_ROOT))


def test_grouped_files_reads_real_markers_and_ignores_fixture_strings(tmp_path):
    from tests.shards import grouped_files

    sources = {
        "module": 'import pytest\npytestmark = pytest.mark.xdist_group("redis")\n',
        "function": 'import pytest\n@pytest.mark.xdist_group(name="sdk")\ndef test_x(): pass\n',
        "fixture": "source = 'pytest.mark.xdist_group(\"fake\")'\n",
        "plain": "def test_x(): pass\n",
    }
    for name, source in sources.items():
        (tmp_path / f"{name}.py").write_text(source)
    assert grouped_files(tmp_path, [f"{name}.py" for name in sources]) == {
        "module.py": "'redis'",
        "function.py": "'sdk'",
    }


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


@pytest.mark.parametrize("worker", [False, True])
def test_shard_option_accounts_for_serial_worker_groups(tmp_path, worker):
    (tmp_path / "tests").mkdir()
    for name in "abcd":
        mark = 'pytestmark = pytest.mark.xdist_group("redis")\n' if name in "ac" else ""
        (tmp_path / "tests" / f"test_{name}.py").write_text("import pytest\n" + mark)
    durations = {f"tests/test_{name}.py::t": seconds for name, seconds in zip("abcd", (5, 9, 5, 8))}
    (tmp_path / ".test_durations").write_text(json.dumps(durations))
    config = SimpleNamespace(
        getoption=lambda name: "1/2",
        stash=pytest.Stash(),
        rootpath=tmp_path,
        option=SimpleNamespace(numprocesses=None if worker else 4),
        **({"workerinput": {"workercount": 4}} if worker else {}),
    )
    assert conftest._shard_files(config) == frozenset({"tests/test_a.py", "tests/test_b.py"})


def test_shard_option_collects_only_that_shards_files(pytestconfig):
    files = [path for path in discover_test_files(_ROOT) if path not in FIRST_SHARD_FILES]
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
    conftest.pytest_collection_modifyitems(
        SimpleNamespace(getoption=lambda name: None, stash=pytest.Stash(), rootpath=tmp_path, **config), items
    )
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
    monkeypatch.setattr(NodeManager, "setup_nodes", NodeManager.setup_nodes)
    monkeypatch.setattr(conftest, "warm_imports", lambda modules, workers: calls.append((modules, workers)) or [7])
    config = SimpleNamespace(
        args=[],
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
    files = [path for path in discover_test_files(_ROOT) if path not in FIRST_SHARD_FILES]
    durations = {node: seconds for node, seconds in _stored_durations().items() if node.split("::", 1)[0] in files}
    part = conftest.assign_nodes(durations, 4, conftest.grouped_files(_ROOT, files), 4)[1]
    known = {node.split("::", 1)[0] for node in durations}
    shard = sorted({node.split("::", 1)[0] for node in part} | (set(files) - known))
    assert calls == [([path.removesuffix(".py").replace("/", ".") for path in shard], 4)]
    reaped = []
    monkeypatch.setattr(conftest.os, "waitpid", lambda pid, flags: reaped.append(pid))
    conftest.pytest_unconfigure(config)
    assert reaped == [7]


def test_workers_and_runs_without_workers_warm_nothing(monkeypatch):
    assert _configured(monkeypatch, 4, workerinput={})[0] == []
    assert _configured(monkeypatch, None)[0] == []


def test_platforms_without_fork_warm_nothing(monkeypatch):
    monkeypatch.delattr(conftest.os, "fork")
    assert _configured(monkeypatch, 4)[0] == []


class _Manager:
    def __init__(self, specs):
        self.specs = specs
        self.events = []
        self.started = threading.Barrier(len(specs), timeout=5)
        self.config = SimpleNamespace(hook=SimpleNamespace(pytest_xdist_setupnodes=self._announce))

    def _announce(self, config, specs):
        self.events.append(("setupnodes", list(specs)))

    def setup_node(self, spec, putevent):
        self.started.wait()
        return (spec, putevent)


def test_the_workers_of_a_shard_are_set_up_side_by_side():
    manager = _Manager(["gw0", "gw1", "gw2", "gw3"])
    put = object()
    assert setup_nodes_in_parallel(manager, put) == [("gw0", put), ("gw1", put), ("gw2", put), ("gw3", put)]
    assert manager.events == [("setupnodes", ["gw0", "gw1", "gw2", "gw3"])]


def test_the_base_temp_exists_once_before_any_worker_starts(tmp_path):
    root = tmp_path / "basetemp"

    class Factory:
        def getbasetemp(self):
            root.mkdir()
            return root

    manager = _Manager(["gw0", "gw1", "gw2", "gw3"])
    manager.config._tmp_path_factory = Factory()
    seen = []
    setup = manager.setup_node
    manager.setup_node = lambda spec, putevent: seen.append(root.is_dir()) or setup(spec, putevent)
    setup_nodes_in_parallel(manager, object())
    assert seen == [True, True, True, True]


def test_the_controller_of_a_sharded_run_sets_up_its_workers_side_by_side(monkeypatch):
    _configured(monkeypatch, 4)
    assert NodeManager.setup_nodes is setup_nodes_in_parallel


def test_workers_and_runs_without_workers_keep_xdists_node_setup(monkeypatch):
    original = NodeManager.setup_nodes
    _configured(monkeypatch, 4, workerinput={})
    assert NodeManager.setup_nodes is original
    _configured(monkeypatch, None)
    assert NodeManager.setup_nodes is original
