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
