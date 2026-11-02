import json
from pathlib import Path

import pytest
from coverage import Coverage, CoverageData

from tests import coverage_ratchet as ratchet

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("arcs", [False, True])
def test_report_reuses_measured_lines_without_copying_test_contexts(tmp_path, monkeypatch, arcs):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/a.py").write_text("def f(x):\n    if x:\n        return 1\n    return 2\n")
    (tmp_path / "hooks/untested.py").write_text("value = 1\n")
    shards = []
    for index, lines in enumerate(([1, 2, 3], [1, 2, 4])):
        shard = tmp_path / f"shard-{index}"
        data = CoverageData(basename=str(shard))
        data.set_context(f"test-{index}|run")
        if arcs:
            data.add_arcs({"hooks/a.py": [(line, line + 1) for line in lines]})
        else:
            data.add_lines({"hooks/a.py": lines})
        data.write()
        shards.append(shard)
    original = CoverageData(basename=str(tmp_path / "original"))
    for shard in shards:
        part = CoverageData(basename=str(shard))
        part.read()
        original.update(part)
    coverage = Coverage(data_file=original.base_filename(), config_file=False)
    coverage.set_option("run:relative_files", True)
    coverage.load()
    monkeypatch.chdir(tmp_path)
    expected = {str(path): sorted(coverage.analysis2(str(path))[3]) for path in Path("hooks").glob("*.py")}
    measured = ratchet.executed(shards)
    (tmp_path / "out").mkdir()

    def refuse_context_merge(*args, **kwargs):
        pytest.fail("the report copied shard contexts after their lines were measured")

    monkeypatch.setattr(CoverageData, "update", refuse_context_merge)
    assert ratchet._missed(tmp_path, measured, tmp_path / "out") == expected


def test_identical_source_is_parsed_once_for_different_measurements(monkeypatch):
    from coverage.exceptions import NotPython

    from tests import coverage_grade

    source = "def parse_reuse_case(x):\n    if x:\n        return 1\n    return 2\n"
    parsed = []
    original = coverage_grade.PythonParser

    def parser(*args, **kwargs):
        parsed.append(kwargs["text"])
        return original(*args, **kwargs)

    monkeypatch.setattr(coverage_grade, "PythonParser", parser)
    assert coverage_grade.statements_of(source, {1, 2, 3}) == {1: 1, 2: 2, 3: 3}
    assert coverage_grade.statements_of(source, {1, 4}) == {1: 1, 4: 4}
    assert parsed == [source]
    changed = source.replace("return 2", "return 3")
    assert coverage_grade.statements_of(changed, {4}) == {4: 4}
    assert parsed == [source, changed]
    with pytest.raises(NotPython):
        coverage_grade.statements_of("def broken(:", {1})


def _write_shards(tmp_path: Path, count: int) -> list[Path]:
    shards = []
    for index in range(count):
        shard = tmp_path / f"parallel-shard-{index}"
        data = CoverageData(basename=str(shard))
        data.add_lines({f"hooks/m{module}.py": [1, 2 + (index + module) % 2] for module in range(4)})
        data.write()
        shards.append(shard)
    return shards


def test_parallel_shard_reads_equal_the_serial_read(tmp_path: Path) -> None:
    shards = _write_shards(tmp_path, 6)
    serial = ratchet.executed(shards, workers=1)
    assert ratchet.executed(shards, workers=2) == serial
    assert serial["hooks/m0.py"] == {1, 2, 3}


def test_parallel_shard_read_still_refuses_an_unmeasured_shard(tmp_path: Path) -> None:
    shards = _write_shards(tmp_path, 2)
    empty = CoverageData(basename=str(tmp_path / "empty"))
    empty.add_lines({"docs/x.py": [1]})
    empty.write()
    with pytest.raises(ratchet.Unmeasured, match="measured no line"):
        ratchet.executed([*shards, tmp_path / "empty"], workers=2)


def test_parallel_missed_report_equals_the_serial_report(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    for module in range(5):
        (tmp_path / f"hooks/m{module}.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/untested.py").write_text("value = 1\n")
    measured = ratchet.executed(_write_shards(tmp_path, 3), workers=1)
    (tmp_path / "serial").mkdir()
    (tmp_path / "parallel").mkdir()
    serial = ratchet._missed(tmp_path, measured, tmp_path / "serial", workers=1)
    parallel = ratchet._missed(tmp_path, measured, tmp_path / "parallel", workers=2)
    assert parallel == serial
    assert list(parallel) == sorted(parallel)
    assert serial["scripts/untested.py"] == [1]
    assert serial["hooks/m3.py"] == [4]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [("max 100000\n", 4), ("200000 100000\n", 2), ("150000 100000\n", 2), ("50000 100000\n", 1), (None, 4)],
)
def test_worker_count_follows_the_cgroup_cpu_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str | None, expected: int
) -> None:
    monkeypatch.setattr(ratchet.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3})
    path = tmp_path / "cpu.max"
    if limit is not None:
        path.write_text(limit)
    assert ratchet._cpus(path) == expected


def test_usage_reads_cpu_seconds_and_throttled_time(tmp_path: Path) -> None:
    stat = tmp_path / "cpu.stat"
    stat.write_text("usage_usec 10\nnr_throttled 3\nthrottled_usec 2500000\n")
    cpu, throttled = ratchet._usage(stat)
    assert cpu > 0
    assert throttled == 2.5
    assert ratchet._usage(tmp_path / "absent")[1] == 0.0


TRICKY = (
    '"""Module docstring."""\n'
    "import os\n"
    "\n"
    "\n"
    "@staticmethod\n"
    "def f(\n"
    "    x,\n"
    "    y,\n"
    "):\n"
    "    total = (\n"
    "        x\n"
    "        + y\n"
    "    )\n"
    "    if total:  # pragma: no cover\n"
    "        return os.sep\n"
    '    text = """a\n'
    'b"""\n'
    "    return [\n"
    "        total,\n"
    "        text,\n"
    "    ]\n"
    "\n"
    "\n"
    "class C:\n"
    "    def g(self):\n"
    "        ...\n"
)


def _statement_tree(tmp_path: Path) -> Path:
    head = tmp_path / "head"
    (head / "hooks/pkg").mkdir(parents=True)
    (head / "scripts").mkdir()
    (head / "hooks/tricky.py").write_text(TRICKY)
    (head / "hooks/pkg/__init__.py").write_text("")
    (head / "hooks/pkg/m.py").write_text("a = 1\nb = [\n    2,\n]\nc = 3\n")
    (head / "scripts/untested.py").write_text("value = 1\n")
    return head


def _summarized_statements(head: Path, parts: int, tmp_path: Path) -> dict[str, dict]:
    from tests import coverage_summary

    written = []
    for part in range(1, parts + 1):
        out = tmp_path / f"statements-{part}" / coverage_summary.STATEMENTS
        out.parent.mkdir()
        coverage_summary.write_statements(head, part, parts, out)
        written.append(out)
    return coverage_summary.statements(written)


def test_missed_lines_from_shard_statement_summaries_match_the_analysis_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _statement_tree(tmp_path)
    measured = {"hooks/tricky.py": {1, 2, 6, 8, 10, 12, 17, 19, 25}, "hooks/pkg/m.py": {1, 3}}
    (tmp_path / "analysed").mkdir()
    expected = ratchet._missed(head, measured, tmp_path / "analysed")
    statements = _summarized_statements(head, 2, tmp_path)
    assert set(statements) == set(expected)

    def refuse_analysis(*args: object, **kwargs: object) -> dict[str, list[int]]:
        pytest.fail("a module the shard summaries cover was analysed again")

    monkeypatch.setattr(ratchet, "_analysed", refuse_analysis)
    (tmp_path / "summarized").mkdir()
    listed = ratchet._missed(head, measured, tmp_path / "summarized", summaries=statements)
    assert listed == expected
    assert list(listed) == list(expected)
    assert expected["scripts/untested.py"] == [1]


def test_a_module_whose_source_differs_from_its_statement_summary_is_analysed(tmp_path: Path) -> None:
    head = _statement_tree(tmp_path)
    statements = _summarized_statements(head, 1, tmp_path)
    (head / "hooks/pkg/m.py").write_text("a = 1\nb = 2\n")
    del statements["scripts/untested.py"]
    measured = {"hooks/pkg/m.py": {1}}
    (tmp_path / "analysed").mkdir()
    (tmp_path / "summarized").mkdir()
    expected = ratchet._missed(head, measured, tmp_path / "analysed")
    assert ratchet._missed(head, measured, tmp_path / "summarized", summaries=statements) == expected
    assert expected["hooks/pkg/m.py"] == [2]
    assert expected["scripts/untested.py"] == [1]


def test_shard_slices_cover_every_graded_module_once(tmp_path: Path) -> None:
    from tests import coverage_summary

    head = _statement_tree(tmp_path)
    seen = []
    for part in range(1, 4):
        out = tmp_path / f"slice-{part}.json"
        coverage_summary.main(
            ["--statements", str(out), "--slice", f"{part}/3", "--head", str(head), "--out", str(tmp_path / "x")]
        )
        seen += list(json.loads(out.read_text()))
    assert sorted(seen) == ratchet.graded_modules(head)


def test_the_ratchet_reads_statement_summaries_beside_the_executed_lines_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import coverage_summary

    head = _statement_tree(tmp_path)
    for part in (1, 2):
        folder = tmp_path / "summaries" / f"executed-lines-3.12-{part}"
        folder.mkdir(parents=True)
        coverage_summary.write_statements(head, part, 2, folder / coverage_summary.STATEMENTS)
    files = sorted((tmp_path / "summaries").glob(f"*/{coverage_summary.STATEMENTS}"))
    assert ratchet._head_statements(tmp_path / "summaries") == coverage_summary.statements(files)
    assert set(ratchet._head_statements(tmp_path / "summaries")) == set(ratchet.graded_modules(head))
    assert ratchet._head_statements(None) == {}
