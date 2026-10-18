import json
import subprocess
from pathlib import Path

import pytest
from coverage import CoverageData
from coverage.exceptions import NotPython

from tests import coverage_history
from tests import coverage_ratchet as ratchet
from tests.coverage_grade import HISTORY

pytestmark = pytest.mark.unit

SOURCE = "def f(x):\n    if x:\n        return 1\n    return 2\n"


def _shard(path: Path, lines: dict[str, list[int]]) -> Path:
    data = CoverageData(basename=str(path))
    data.add_lines(lines)
    data.write()
    return path


def _measure(commit, executed, sources):
    return ratchet.Measurement(commit, executed, lambda path: sources.get(path))


def test_unchanged_lines_map_across_an_insert_above_them():
    old = "a\nb\nc\n"
    new = "a\nnew\nb\nc\n"
    assert ratchet.line_map(old, new) == {1: 1, 2: 3, 3: 4}


def test_shards_union_their_executed_lines_under_the_graded_trees(tmp_path):
    one = _shard(tmp_path / "one", {"hooks/a.py": [1, 2], "tests/t.py": [1]})
    two = _shard(tmp_path / "two", {"hooks/a.py": [3], "scripts/b.py": [5]})
    assert ratchet.executed([one, two]) == {"hooks/a.py": {1, 2, 3}, "scripts/b.py": {5}}


def test_a_shard_that_measured_nothing_cannot_be_graded(tmp_path):
    one = _shard(tmp_path / "one", {"hooks/a.py": [1]})
    empty = _shard(tmp_path / "empty", {"tests/t.py": [1]})
    with pytest.raises(ratchet.Unmeasured, match="empty"):
        ratchet.executed([one, empty])


def test_a_line_the_base_ran_and_the_head_no_longer_runs_is_lost():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter([base]))
    assert result.base == "b1"
    assert result.lost == {"hooks/a.py": [3]}


@pytest.mark.parametrize("head_lines", [199, 200, 500])
@pytest.mark.parametrize("inserted", [False, True])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_repeated_retained_statements_still_lose_coverage(head_lines, inserted, split, missing):
    old = "def retained():\n" + "    value = 1\n" * (head_lines - 1 - inserted)
    new = old.replace("def retained():\n", "def retained():\n    inserted = 0\n") if inserted else old
    other = "def other():\n    return 2\n"
    path = "hooks/old.py"
    target = "hooks/retained.py" if split else path
    sources = {target: new, "hooks/other.py": other} if split else {target: new}
    base = _measure("b1", {path: set(range(1, head_lines + 1 - inserted))}, {path: old + other if split else old})
    covered = set(range(1, head_lines + 1)) - ({100 + inserted} if missing else set())
    moved = {path: (target, "hooks/other.py")} if split else None
    result = ratchet.grade({target: covered}, sources.get, iter([base]), moved)
    assert result.lost == ({path: [100]} if missing else {})


@pytest.mark.wall_clock
def test_long_repeated_line_mapping_fits_the_ratchet_budget(capsys):
    import sys
    from time import perf_counter

    old = "    value = 1\n" * 3000
    new = "    inserted = 0\n" + old
    started = perf_counter()
    mapped = ratchet.line_map(old, new)
    elapsed = perf_counter() - started
    with capsys.disabled():
        print(f"Repeated line mapping: lines=3000 seconds={elapsed:.6f} job_budget_seconds=600", file=sys.stderr)
    assert mapped == {line: line + 1 for line in range(1, 3001)}
    assert elapsed < 10


def test_a_base_line_that_is_no_statement_of_the_base_source_is_never_lost():
    source = 'SCRIPT = """\nconst a = 1;\n"""\n\n\ndef f(x):\n    return x\n'
    base = _measure("b1", {"hooks/a.py": {1, 2, 6, 7}}, {"hooks/a.py": source})
    result = ratchet.grade({"hooks/a.py": {1, 6}}, lambda path: source, iter([base]))
    assert result.lost == {"hooks/a.py": [7]}


def test_a_statement_the_head_runs_only_through_its_continuation_lines_is_kept():
    source = "x = max(\n    1,\n    2,\n)\n"
    base = _measure("b1", {"hooks/a.py": {1}}, {"hooks/a.py": source})
    result = ratchet.grade({"hooks/a.py": {2, 3}}, lambda path: source, iter([base]))
    assert result.lost == {}


def test_a_statement_whose_first_line_changed_is_lost_when_the_head_stops_running_it():
    old = "v = max(\n    1,\n    2,\n)\n"
    new = "v = min(\n    1,\n    2,\n)\n"
    base = _measure("b1", {"hooks/a.py": {2, 3}}, {"hooks/a.py": old})
    result = ratchet.grade({"hooks/a.py": set()}, lambda path: new, iter([base]))
    assert result.lost == {"hooks/a.py": [1]}


def test_history_that_ran_a_statement_only_through_continuation_lines_keeps_it_lost():
    source = "v = max(\n    1,\n    2,\n)\n"
    runs = [
        _measure(commit, {"hooks/a.py": lines}, {"hooks/a.py": source})
        for commit, lines in [("b1", {1, 2, 3}), ("b2", {2, 3}), ("b3", {2, 3})]
    ]
    result = ratchet.grade({"hooks/a.py": set()}, lambda path: source, iter(runs))
    assert result.lost == {"hooks/a.py": [1]}
    assert result.unconfirmed == {}


def test_an_unparsable_source_cannot_be_graded():
    base = _measure("b1", {"hooks/a.py": {1}}, {"hooks/a.py": "def (\n"})
    with pytest.raises(NotPython):
        ratchet.grade({"hooks/a.py": set()}, lambda path: "def (\n", iter([base]))


def test_an_added_line_no_test_runs_is_not_a_lost_line():
    head_source = SOURCE + "\n\ndef g():\n    return 3\n"
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    result = ratchet.grade({"hooks/a.py": {1, 2, 3, 7}}, lambda path: head_source, iter([base]))
    assert result.lost == {}


def test_a_deleted_module_loses_nothing():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    assert ratchet.grade({}, lambda path: None, iter([base])).lost == {}


def test_a_renamed_module_keeps_every_line_the_base_ran():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    sources = {"hooks/b.py": SOURCE}
    result = ratchet.grade({"hooks/b.py": {1, 2}}, sources.get, iter([base]), {"hooks/a.py": "hooks/b.py"})
    assert result.lost == {"hooks/a.py": [3]}


def test_a_module_the_head_stopped_measuring_loses_every_line_it_still_has():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    assert ratchet.grade({}, lambda path: SOURCE, iter([base])).lost == {"hooks/a.py": [1, 2, 3]}


MOVED = SOURCE + "\n\ndef h():\n    return 0\n"
REWRITTEN = "import os\n\n\n" + MOVED + "".join(f"\n\ndef g{n}():\n    return os.sep * {n}\n" for n in range(12))
UNRELATED = "def other():\n    return 0\n"


def test_a_module_moved_and_rewritten_pairs_with_the_added_module_sharing_its_definitions():
    moves = ratchet.pair_moves({"hooks/a.py": MOVED}, {"hooks/z.py": UNRELATED, "scripts/b.py": REWRITTEN})
    assert moves == {"hooks/a.py": "scripts/b.py"}


def test_a_deleted_module_pairs_with_no_unrelated_added_module():
    assert ratchet.pair_moves({"hooks/a.py": MOVED}, {"hooks/z.py": UNRELATED, "hooks/broken.py": "def ("}) == {}


def _module(bodies: dict[str, str]) -> str:
    return "import os\nimport sys\n" + "".join(f"\n\ndef {name}():\n    {body}\n" for name, body in bodies.items())


def test_a_move_keeping_its_definitions_pairs_however_much_of_its_bodies_was_rewritten():
    old = _module({f"step{n}": f"return {n}" for n in range(6)})
    new = _module({f"step{n}": f"return {n}" if n < 2 else f"return os.sep * {n}" for n in range(6)})
    assert ratchet.pair_moves({"hooks/a.py": old}, {"scripts/b.py": new}) == {"hooks/a.py": "scripts/b.py"}


def test_a_lone_generic_name_or_a_minority_of_names_pairs_nothing():
    old = _module({"main": "sys.exit(1)", "helper": "return 2", "other": "return 3"})
    assert ratchet.pair_moves({"hooks/old.py": old}, {"scripts/new.py": _module({"main": "sys.exit(1)"})}) == {}
    lone = _module({"main": "sys.exit(1)"})
    assert ratchet.pair_moves({"hooks/old.py": lone}, {"scripts/new.py": _module({"main": "sys.exit(0)"})}) == {}


def test_names_other_head_modules_also_define_never_pair_a_module():
    old = _module({"main": "sys.exit(1)", "run": "return 1"})
    new = _module({"main": "sys.exit(0)", "run": "return 0"})
    elsewhere = _module({"main": "return 2", "run": "return 2"})
    moves = ratchet.pair_moves({"hooks/old_cli.py": old}, {"scripts/new_cli.py": new}, [new, elsewhere])
    assert moves == {}
    assert ratchet.pair_moves({"hooks/old_cli.py": old}, {"scripts/new_cli.py": new}, [new]) == {
        "hooks/old_cli.py": "scripts/new_cli.py"
    }


def _repo(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)


def test_a_move_rewritten_below_git_rename_similarity_is_followed(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _repo(tmp_path, {"hooks/a.py": MOVED, "hooks/gone.py": UNRELATED, "tests/t.py": SOURCE})
    for name in ("hooks/a.py", "hooks/gone.py", "tests/t.py"):
        (tmp_path / name).unlink()
    _repo(tmp_path, {"scripts/b.py": REWRITTEN, "tests/u.py": REWRITTEN})
    listed = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--name-status", "-M", "HEAD~1", "HEAD"], capture_output=True, text=True
    ).stdout
    assert not any(line.startswith("R") for line in listed.splitlines())
    assert coverage_history.renamed(tmp_path, "HEAD~1") == {"hooks/a.py": "scripts/b.py"}
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": MOVED})
    head_source = {"scripts/b.py": REWRITTEN}.get
    moved = coverage_history.renamed(tmp_path, "HEAD~1")
    assert ratchet.grade({"scripts/b.py": {4, 5}}, head_source, iter([base]), moved).lost == {"hooks/a.py": [3]}


@pytest.mark.parametrize("missing", [None, "first", "second"])
@pytest.mark.parametrize("rewritten", [False, True])
def test_a_split_module_grades_each_destination_by_its_own_definitions(tmp_path, missing, rewritten):
    old = "def first():\n    return 1\n\ndef second():\n    return 1\n"
    sources = {
        f"hooks/{name}.py": ("import os\n" * 10 if rewritten else "") + f"def {name}():\n    return 1\n"
        for name in ("first", "second")
    }
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _repo(tmp_path, {"hooks/old.py": old})
    (tmp_path / "hooks/old.py").unlink()
    _repo(tmp_path, sources)
    moved = coverage_history.renamed(tmp_path, "HEAD~1")
    base = _measure("b1", {"hooks/old.py": {1, 2, 4, 5}}, {"hooks/old.py": old})
    offset = 10 if rewritten else 0
    head = {path: {offset + 1} | ({offset + 2} if path != f"hooks/{missing}.py" else set()) for path in sources}
    result = ratchet.grade(head, sources.get, iter([base]), moved)
    assert result.lost == ({} if missing is None else {"hooks/old.py": [2 if missing == "first" else 5]})


@pytest.mark.parametrize("missing", [None, "First", "Second"])
def test_split_classes_keep_same_named_methods_with_their_own_class(missing):
    sources = {
        f"hooks/{name}.py": f"class {name}:\n    def run(self):\n        return 1\n" for name in ("First", "Second")
    }
    old = "\n".join(sources.values())
    moved = ratchet.pair_moves({"hooks/old.py": old}, sources, sources.values())
    base = _measure("b1", {"hooks/old.py": {1, 2, 3, 5, 6, 7}}, {"hooks/old.py": old})
    head = {path: {1, 2} | ({3} if path != f"hooks/{missing}.py" else set()) for path in sources}
    result = ratchet.grade(head, sources.get, iter([base]), moved)
    assert result.lost == ({} if missing is None else {"hooks/old.py": [3 if missing == "First" else 7]})


def test_split_definitions_inside_module_conditionals_keep_their_coverage():
    sources = {
        "hooks/first.py": "if True:\n    def first():\n        return 1\n",
        "hooks/second.py": "def second():\n    return 2\n",
    }
    old = "\n".join(sources.values())
    base = _measure("b1", {"hooks/old.py": {1, 2, 3, 5, 6}}, {"hooks/old.py": old})
    moved = ratchet.pair_moves({"hooks/old.py": old}, sources, sources.values())
    result = ratchet.grade({"hooks/first.py": {1, 2}, "hooks/second.py": {1, 2}}, sources.get, iter([base]), moved)
    assert result.lost == {"hooks/old.py": [3]}


def test_a_line_older_runs_missed_after_an_even_older_run_covered_it_is_cleared():
    runs = [
        _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
        _measure("b2", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
        _measure("b3", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
    ]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {}
    assert result.unstable == {"hooks/a.py": [3]}


def test_a_line_first_covered_recently_stays_graded():
    runs = [
        _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
        _measure("b2", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
    ]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {"hooks/a.py": [3]}
    assert result.unstable == {}
    assert getattr(result, "unconfirmed", {}) == {}


@pytest.mark.parametrize("snapshots", [2, 3])
def test_a_base_only_line_with_two_measured_misses_is_unconfirmed(snapshots):
    runs = [_measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}) for n in range(snapshots)]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert (result.lost, result.unstable, getattr(result, "unconfirmed", {})) in (
        ({"hooks/a.py": [3]}, {}, {}),
        ({}, {}, {"hooks/a.py": {3: snapshots}}),
    )


@pytest.mark.parametrize("snapshots", [0, 1, 2, 3])
def test_incidental_classification_requires_two_measured_misses(snapshots):
    runs = [_measure("base", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}) for n in range(snapshots)]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == ({"hooks/a.py": [3]} if snapshots < 2 else {})
    assert result.unconfirmed == ({"hooks/a.py": {3: snapshots}} if snapshots >= 2 else {})


def test_unconfirmed_counts_take_precedence_over_different_version_clearance():
    runs = [_measure("base", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}) for n in range(2)]
    runs.append(_measure("different", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE + "\n# different version\n"}))
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {}
    assert result.unstable == {}
    assert result.unconfirmed == {"hooks/a.py": {3: 2}}


@pytest.mark.parametrize("history,count", [([False, True, False], 2), ([False, False, True, False], 3)])
def test_other_version_coverage_does_not_truncate_same_version_snapshot_count(history, count):
    runs = [_measure("base", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})]
    runs += [
        _measure(
            f"older{n}",
            {"hooks/a.py": {1, 2, 3} if covered else {1, 2}},
            {"hooks/a.py": SOURCE + "\n# different version\n" if covered else SOURCE},
        )
        for n, covered in enumerate(history)
    ]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {}
    assert result.unstable == {}
    assert result.unconfirmed == {"hooks/a.py": {3: count}}


@pytest.mark.parametrize("covered,snapshots", [(True, 20), (False, 1), (False, 2), (False, 20)])
def test_ledger_stall_fixture_grades_each_history_case(covered, snapshots):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "coverage_incidental.json").read_text())
    path, source = fixture["path"], fixture["source"]
    runs = [_measure(fixture["commit"], {path: {315}}, {path: source})]
    runs += [
        _measure(snapshot["commit"], {path: {315} if covered else set()}, {path: source})
        for snapshot in fixture["history"][:snapshots]
    ]
    result = ratchet.grade({path: set()}, lambda name: source, iter(runs))
    assert result.lost == ({path: [315]} if covered or snapshots < 2 else {})
    assert result.unstable == {}
    assert result.unconfirmed == ({path: {315: snapshots}} if not covered and snapshots >= 2 else {})
    text = ratchet.report(result, {path: [315]}, {path: 0}, {path: 1})
    if result.unconfirmed:
        assert f"{path}:315 unconfirmed: {snapshots} measured history snapshots did not run it" in text


def test_the_ledger_hook_baseline_line_accepts_old_or_unconfirmed_grading():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "coverage_incidental.json").read_text())
    path = fixture["path"]
    source = fixture["source"]
    runs = [_measure(fixture["commit"], {path: set(fixture["executed"])}, {path: source})]
    runs += [
        _measure(snapshot["commit"], {path: set(snapshot["executed"])}, {path: source})
        for snapshot in fixture["history"]
    ]
    result = ratchet.grade({path: set()}, lambda name: source, iter(runs))
    assert result.base == "8b25f4ed9fcd9196de364cf44e7a21ee1ed2c446"
    assert (result.lost, result.unstable, getattr(result, "unconfirmed", {})) in (
        ({"scripts/swarm_ledger/ledger_hook.py": [315]}, {}, {}),
        ({}, {}, {"scripts/swarm_ledger/ledger_hook.py": {315: 20}}),
    )


@pytest.mark.parametrize("measured", [False, True])
def test_missing_history_measurements_never_count_as_missed_lines(measured):
    runs = [_measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})]
    if measured:
        runs.append(_measure("b2", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}))
    runs += [_measure(f"absent{n}", {}, {"hooks/a.py": SOURCE}) for n in range(3)]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {"hooks/a.py": [3]}
    assert result.unstable == {}
    assert getattr(result, "unconfirmed", {}) == {}


@pytest.mark.parametrize(
    "older_source", [None, SOURCE.replace("return 1", "return 9"), SOURCE + "\n# another version\n"]
)
def test_unmappable_history_never_confirms_an_incidental_line(older_source):
    runs = [_measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, {"hooks/a.py": older_source}) for n in range(3)]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {"hooks/a.py": [3]}
    assert result.unstable == {}
    assert getattr(result, "unconfirmed", {}) == {}


def test_history_that_ran_the_lost_line_keeps_the_regression_red():
    runs = [
        _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
        _measure("b2", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
        _measure("b3", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
        _measure("b4", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
    ]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {"hooks/a.py": [3]}
    assert result.unstable == {}
    assert getattr(result, "unconfirmed", {}) == {}


@pytest.mark.parametrize("test_source", ["import hooks.a", "from hooks.a import f", "from hooks import a"])
@pytest.mark.parametrize("head_test", [None, "assert True\n", "assert False\n"])
def test_changed_or_deleted_importing_tests_keep_incidental_lines_red(test_source, head_test):
    test_path = "tests/test_a.py"
    base_sources = {"hooks/a.py": SOURCE, test_path: test_source + "\nassert True\n"}
    head_source = test_source + "\n" + head_test if head_test == "assert False\n" else head_test
    head_sources = {"hooks/a.py": SOURCE, test_path: head_source}
    runs = [_measure("b1", {"hooks/a.py": {1, 2, 3}}, base_sources)]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, base_sources) for n in range(2)]
    options = {"changed_tests": {test_path}} if hasattr(ratchet.Result("b1", {}), "unconfirmed") else {}
    result = ratchet.grade({"hooks/a.py": {1, 2}}, head_sources.get, iter(runs), **options)
    assert result.lost == {"hooks/a.py": [3]}
    assert result.unstable == {}
    assert getattr(result, "unconfirmed", {}) == {}


@pytest.mark.parametrize("test_source", ["import hooks.a", "from hooks.a import f", "from hooks import a"])
def test_an_import_added_in_a_changed_head_test_keeps_the_line_red(test_source):
    test_path = "tests/test_a.py"
    base_sources = {"hooks/a.py": SOURCE, test_path: "assert True\n"}
    head_sources = {"hooks/a.py": SOURCE, test_path: test_source + "\nassert True\n"}
    runs = [_measure("b1", {"hooks/a.py": {1, 2, 3}}, base_sources)]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, base_sources) for n in range(2)]
    options = {"changed_tests": {test_path}} if hasattr(ratchet.Result("b1", {}), "unconfirmed") else {}
    result = ratchet.grade({"hooks/a.py": {1, 2}}, head_sources.get, iter(runs), **options)
    assert result.lost == {"hooks/a.py": [3]}
    assert result.unstable == {}
    assert getattr(result, "unconfirmed", {}) == {}


def test_changing_an_unrelated_test_does_not_confirm_the_line():
    test_path = "tests/test_b.py"
    sources = {"hooks/a.py": SOURCE, test_path: "from hooks.b import f\nassert True\n"}
    runs = [_measure("b1", {"hooks/a.py": {1, 2, 3}}, sources)]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, sources) for n in range(2)]
    options = {"changed_tests": {test_path}} if hasattr(ratchet.Result("b1", {}), "unconfirmed") else {}
    result = ratchet.grade({"hooks/a.py": {1, 2}}, sources.get, iter(runs), **options)
    assert (result.lost, result.unstable, getattr(result, "unconfirmed", {})) in (
        ({"hooks/a.py": [3]}, {}, {}),
        ({}, {}, {"hooks/a.py": {3: 2}}),
    )


def test_history_is_read_only_when_a_line_was_lost():
    def runs():
        yield _measure("b1", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE})
        raise AssertionError("history read without a lost line")

    assert ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, runs()).lost == {}


def test_history_stops_once_every_lost_line_is_cleared():
    read = []

    def runs():
        for commit, ran in (("b1", {1, 2, 3}), ("b2", {1, 2}), ("b3", {1, 2, 3}), ("b4", {1, 2, 3})):
            read.append(commit)
            yield _measure(commit, {"hooks/a.py": ran}, {"hooks/a.py": SOURCE})

    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, runs())
    assert result.unstable == {"hooks/a.py": [3]}
    assert read == ["b1", "b2", "b3"]


def _history(monkeypatch, passed=lambda commit: True, downloaded=lambda commit: True):
    from tests import coverage_history

    commits = [f"c{n}" for n in range(coverage_history.SEARCH)]
    looked_up = []

    def passed_run(repo, commit):
        looked_up.append(commit)
        return f"run-{commit}" if passed(commit) else None

    def fetcher(repo, shards, scratch, read):
        return lambda pair: _measure(pair[0], {}, {}) if downloaded(pair[0]) else None

    monkeypatch.setattr(coverage_history, "git", lambda *args, cwd: "\n".join(commits))
    monkeypatch.setattr(coverage_history, "_passed_run", passed_run)
    monkeypatch.setattr(coverage_history, "_fetcher", fetcher)
    return coverage_history.dev_runs(Path("."), "c0", 8, Path(".")), looked_up


def test_the_base_run_costs_one_github_lookup(monkeypatch):
    runs, looked_up = _history(monkeypatch)
    assert next(runs).commit == "c0"
    assert looked_up == ["c0"]
    runs.close()


def test_a_base_without_a_passed_run_reads_nothing_older(monkeypatch):
    runs, looked_up = _history(monkeypatch, passed=lambda commit: commit != "c0")
    assert list(runs) == []
    assert looked_up == ["c0"]


def test_history_counts_every_passed_run_it_tried_to_download(monkeypatch):
    failed = {f"c{n}" for n in range(1, 6)}
    runs, _ = _history(monkeypatch, downloaded=lambda commit: commit not in failed)
    assert [run.commit for run in runs] == ["c0"] + [f"c{n}" for n in range(6, HISTORY + 1)]


def test_a_line_no_older_run_clears_reads_the_whole_history():
    read = []

    def runs():
        for n in range(HISTORY + 10):
            read.append(n)
            yield _measure(f"b{n}", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})

    assert ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, runs()).lost == {"hooks/a.py": [3]}
    assert len(read) == HISTORY + 1


def test_no_measured_base_cannot_be_graded():
    with pytest.raises(ratchet.Unmeasured, match="base"):
        ratchet.grade({"hooks/a.py": {1}}, lambda path: SOURCE, iter([]))


def test_the_report_lists_every_missed_line_and_every_lost_line(tmp_path):
    result = ratchet.Result("b1", {"hooks/a.py": [3]}, {})
    text = ratchet.report(result, {"hooks/a.py": [3, 4]}, {"hooks/a.py": 2}, {"hooks/a.py": 3})
    assert "hooks/a.py: covered 2 (base 3), missed 3, 4" in text
    assert "hooks/a.py:3 ran on base b1 and no head test runs it" in text


@pytest.mark.parametrize("snapshots", [0, 1, 2, 3])
def test_command_reports_unconfirmed_history_counts(tmp_path, monkeypatch, snapshots):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    sources = {"hooks/a.py": SOURCE}
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    _repo(tmp_path, sources)
    base = coverage_history.git("rev-parse", "HEAD", cwd=tmp_path).strip()
    runs = [_measure(base, {"hooks/a.py": {1, 2, 3}}, sources)]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, sources) for n in range(snapshots)]
    monkeypatch.setattr(ratchet, "_cached_runs", lambda *args: iter(runs))
    shards = tmp_path / "shards" / "coverage-3.12-1"
    shards.mkdir(parents=True)
    _shard(shards / ".coverage", {"hooks/a.py": [1, 2]})
    out = tmp_path / "out"
    status = ratchet.main(
        [
            "--base",
            base,
            "--head",
            str(tmp_path),
            "--shards",
            "1",
            "--head-shards",
            str(shards.parent),
            "--out",
            str(out),
            "--base-baseline",
            "fixture.json",
        ]
    )
    assert status == (1 if snapshots < 2 else 0)
    text = (out / "report.txt").read_text()
    if snapshots >= 2:
        assert f"hooks/a.py:3 unconfirmed: {snapshots} measured history snapshots did not run it" in text
    else:
        assert "hooks/a.py:3 ran on base" in text


@pytest.mark.parametrize("change", ["unchanged", "deleted", "removed", "added", "unrelated", "renamed"])
def test_command_keeps_changed_importing_tests_red(tmp_path, monkeypatch, change):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    test = "tests/test_a.py"
    imports = "import hooks.a\nassert True\n"
    base_test = "assert True\n" if change in {"added", "unrelated"} else imports
    sources = {"hooks/a.py": SOURCE, test: base_test}
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    _repo(tmp_path, sources)
    base = coverage_history.git("rev-parse", "HEAD", cwd=tmp_path).strip()
    if change == "deleted":
        (tmp_path / test).unlink()
    elif change == "renamed":
        (tmp_path / test).rename(tmp_path / "tests/test_renamed.py")
    elif change != "unchanged":
        (tmp_path / test).write_text(
            "import hooks.b\n" if change == "unrelated" else imports if change == "added" else "assert True\n"
        )
    if change != "unchanged":
        subprocess.run(["git", "-C", str(tmp_path), "add", "tests"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change tests"],
            check=True,
            capture_output=True,
        )
    runs = [_measure(base, {"hooks/a.py": {1, 2, 3}}, sources)]
    runs += [_measure(f"older{n}", {"hooks/a.py": {1, 2}}, sources) for n in range(2)]
    monkeypatch.setattr(ratchet, "_cached_runs", lambda *args: iter(runs))
    shards = tmp_path / "shards" / "coverage-3.12-1"
    shards.mkdir(parents=True)
    _shard(shards / ".coverage", {"hooks/a.py": [1, 2]})
    out = tmp_path / "out"
    status = ratchet.main(
        [
            "--base",
            base,
            "--head",
            str(tmp_path),
            "--shards",
            "1",
            "--head-shards",
            str(shards.parent),
            "--out",
            str(out),
            "--base-baseline",
            "fixture.json",
        ]
    )
    assert status == (0 if change in {"unchanged", "unrelated"} else 1)
    text = (out / "report.txt").read_text()
    if status:
        assert "hooks/a.py:3 ran on base" in text
    else:
        assert "hooks/a.py:3 unconfirmed: 2 measured history snapshots did not run it" in text


@pytest.mark.parametrize("queued_before", [False, True])
@pytest.mark.parametrize("event", ["merge_group", "pull_request"])
@pytest.mark.parametrize("changed", ["hooks/a.py", "hooks/b.py", "README.md", None])
def test_queue_ratchet_only_fails_losses_in_the_entry_diff(tmp_path, monkeypatch, event, changed, queued_before):
    sources = {"hooks/a.py": SOURCE, "hooks/b.py": SOURCE, "README.md": "base\n"}
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    _repo(tmp_path, sources)
    base = coverage_history.git("rev-parse", "HEAD", cwd=tmp_path).strip()
    if queued_before:
        _repo(tmp_path, {"hooks/a.py": SOURCE + "\n"})
    if changed:
        _repo(tmp_path, {changed: sources[changed] + "\n\n"})
    else:
        coverage_history.git(
            "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "entry", cwd=tmp_path
        )
    runs = [_measure(base, {"hooks/a.py": {1, 2, 3}, "hooks/b.py": {1, 2, 3}}, sources)]
    monkeypatch.setattr(ratchet, "_cached_runs", lambda *args: iter(runs))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    shards = tmp_path / "shards" / "coverage-3.12-1"
    shards.mkdir(parents=True)
    _shard(shards / ".coverage", {"hooks/a.py": [1, 2], "hooks/b.py": [1, 2]})
    out = tmp_path / "out"
    status = ratchet.main(
        [
            "--base",
            base,
            "--head",
            str(tmp_path),
            "--shards",
            "1",
            "--head-shards",
            str(shards.parent),
            "--out",
            str(out),
            "--base-baseline",
            "fixture.json",
        ]
    )
    expected = {"hooks/a.py", "hooks/b.py"} if event == "pull_request" else {changed} & {"hooks/a.py", "hooks/b.py"}
    assert status == int(bool(expected))
    text = (out / "report.txt").read_text()
    for path in ("hooks/a.py", "hooks/b.py"):
        assert (f"{path}:3 ran on base" in text) == (path in expected)


def test_the_report_lists_only_lines_no_head_shard_ran(tmp_path):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/a.py").write_text(SOURCE)
    (tmp_path / "out").mkdir()
    assert ratchet._missed(tmp_path, {"hooks/a.py": {1, 2, 3}}, tmp_path / "out") == {"hooks/a.py": [4]}


def test_a_failed_github_read_is_retried_before_the_gate_gives_up(monkeypatch):
    import subprocess

    from tests import coverage_history

    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        code = 1 if len(calls) < 3 else 0
        return subprocess.CompletedProcess(cmd, code, stdout="42\n", stderr="HTTP 502")

    monkeypatch.setattr(coverage_history.subprocess, "run", run)
    monkeypatch.setattr(coverage_history.time, "sleep", lambda seconds: None)
    assert coverage_history._gh("api", "x") == "42"
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(
        coverage_history.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
    )
    with pytest.raises(subprocess.CalledProcessError):
        coverage_history._gh("api", "x")
