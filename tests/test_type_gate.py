import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import type_gate

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

ILL_TYPED = "def planted(value: int) -> str:\n    return value\n"
WELL_TYPED = "def planted(value: int) -> str:\n    return str(value)\n"
_SETTINGS = "\n".join(f"{key} = {json.dumps(value)}" for key, value in type_gate.CONFIG.items())
PYPROJECT = f"[tool.basedpyright]\n{_SETTINGS}\n"


def _tree(root: Path, files: dict[str, str], baseline: dict[str, int] | None = None) -> Path:
    for name, text in {"pyproject.toml": PYPROJECT, "hooks/__init__.py": "", **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if baseline is not None:
        (root / "tests").mkdir(exist_ok=True)
        (root / type_gate.BASELINE).write_text(json.dumps(baseline))
    return root


def _grade(base: Path, head: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = type_gate.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


def test_the_grader_pins_the_basedpyright_the_tests_install() -> None:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert f"basedpyright=={type_gate.BASEDPYRIGHT_VERSION}" in dev
    assert pyproject["tool"]["basedpyright"] == type_gate.CONFIG
    assert type_gate._basedpyright_version() == type_gate.BASEDPYRIGHT_VERSION


def test_the_config_pins_version_platform_and_strict_scope() -> None:
    assert type_gate.CONFIG == {
        "include": ["hooks", "scripts", "tests"],
        "pythonVersion": "3.12",
        "pythonPlatform": "Linux",
        "typeCheckingMode": "standard",
        "strict": ["hooks", "scripts"],
    }


def test_the_baseline_moves_only_with_the_type_gate_s_evidence() -> None:
    from scripts import threshold_pins

    assert (str(type_gate.BASELINE), "scripts.type_gate") in threshold_pins.GRADED_BY


def test_a_new_ill_typed_function_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {}, {})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED}, {})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::hooks/planted.py has 1 type errors, more than the 0 the base allows\n" in out
    assert (
        f"::error::{type_gate.BASELINE} records 0 type errors in hooks/planted.py, the head has 1; "
        "run python -m scripts.type_gate --write\n"
    ) in out


def test_a_fixed_error_with_a_stale_baseline_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 1})
    head = _tree(tmp_path / "head", {"hooks/planted.py": WELL_TYPED}, {"hooks/planted.py": 1})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out == (
        "0 type errors in 0 files, 1 allowed, 1 errors\n"
        f"::error::{type_gate.BASELINE} records 1 type errors in hooks/planted.py, the head has 0; "
        "run python -m scripts.type_gate --write\n"
    )


def test_a_regenerated_baseline_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 1})
    head = _tree(tmp_path / "head", {"hooks/planted.py": WELL_TYPED}, {"hooks/planted.py": 1})
    assert type_gate.main(["--write", "--head", str(head)]) == 0
    assert json.loads((head / type_gate.BASELINE).read_text()) == {}
    code, out = _grade(base, head, capsys)
    assert (code, out) == (0, "0 type errors in 0 files, 1 allowed, 0 errors\n")


def test_an_unchanged_error_on_the_baseline_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {}, {"hooks/planted.py": 1})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 1})
    code, out = _grade(base, head, capsys)
    measured = tmp_path / "out" / "deep" / "TYPE_BASELINE.json"
    for _ in range(2):
        assert type_gate.main(["--base", str(base), "--head", str(head), "--measured", str(measured)]) == 0
        assert measured.read_text() == '{\n  "hooks/planted.py": 1\n}\n'
    capsys.readouterr()
    assert (code, out) == (0, "1 type errors in 1 files, 1 allowed, 0 errors\n")


def test_tests_are_checked_in_standard_mode(tmp_path: Path) -> None:
    untyped = "def planted(value):\n    return value\n"
    head = _tree(tmp_path, {"hooks/planted.py": untyped, "tests/test_planted.py": untyped})
    measured = type_gate.measure(head)
    assert measured.get("tests/test_planted.py", 0) == 0
    assert measured["hooks/planted.py"] >= 1


def test_a_base_without_a_baseline_grades_the_head_against_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _tree(tmp_path / "base", {})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 1})
    code, out = _grade(base, head, capsys)
    assert (code, out) == (
        0,
        f"::notice::The base carries no {type_gate.BASELINE}; this head introduces it.\n"
        "1 type errors in 1 files, 1 allowed, 0 errors\n",
    )


def test_weakened_settings_are_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {}, {})
    head = _tree(tmp_path / "head", {}, {})
    (head / "pyproject.toml").write_text(PYPROJECT.replace('strict = ["hooks", "scripts"]', "strict = []"))
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out.startswith("::error::pyproject.toml [tool.basedpyright] is ")
    assert out.endswith(f"; the type gate grades only with {type_gate.CONFIG!r}\n")


def test_a_wrong_basedpyright_version_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(type_gate, "_basedpyright_version", lambda: "")
    code = type_gate.main(["--write", "--head", str(tmp_path)])
    assert (code, capsys.readouterr().out) == (
        1,
        f"::error::The type gate needs basedpyright {type_gate.BASEDPYRIGHT_VERSION}, found none.\n",
    )


def test_a_failed_checker_run_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _tree(tmp_path / "base", {}, {})
    head = _tree(tmp_path / "head", {"hooks/planted.py": WELL_TYPED}, {})
    monkeypatch.setattr(type_gate, "_basedpyright_version", lambda: type_gate.BASEDPYRIGHT_VERSION)
    monkeypatch.setattr(type_gate, "_BASEDPYRIGHT", (*type_gate._BASEDPYRIGHT, "--no-such-flag"))
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out.startswith("::error::basedpyright exited ")


def test_grading_needs_a_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exited:
        type_gate.main([])
    assert exited.value.code == 2
    assert capsys.readouterr().err.endswith(" error: grading needs --base\n")


def test_grade_reports_growth_and_drift_per_file() -> None:
    assert type_gate.grade({"a.py": 2}, {"a.py": 2, "b.py": 1}, {"a.py": 2, "b.py": 1}) == [
        "b.py has 1 type errors, more than the 0 the base allows"
    ]
    assert type_gate.grade({"a.py": 2}, {"a.py": 1}, {"a.py": 2}) == [
        f"{type_gate.BASELINE} records 2 type errors in a.py, the head has 1; run python -m scripts.type_gate --write"
    ]
    assert type_gate.grade({"a.py": 2}, {"a.py": 3}, None) == [
        "a.py has 3 type errors, more than the 2 the base allows",
        f"{type_gate.BASELINE} records 0 type errors in a.py, the head has 3; run python -m scripts.type_gate --write",
    ]


def test_grade_holds_drift_only_on_touched_files_and_edited_entries() -> None:
    drift = (
        f"{type_gate.BASELINE} records 2 type errors in a.py, the head has 1; run python -m scripts.type_gate --write"
    )
    assert type_gate.grade({"a.py": 2}, {"a.py": 1}, {"a.py": 2}, set()) == []
    assert type_gate.grade({"a.py": 2}, {"a.py": 1}, {"a.py": 2}, {"a.py"}) == [drift]
    assert type_gate.grade({"a.py": 3}, {"a.py": 1}, {"a.py": 2}, set()) == [drift]
    assert type_gate.grade({"a.py": 1}, {"a.py": 1}, {}, set()) == [
        f"{type_gate.BASELINE} records 0 type errors in a.py, the head has 1; run python -m scripts.type_gate --write"
    ]
    assert type_gate.grade({"a.py": 2}, {"a.py": 3}, {"a.py": 2}, {"a.py"}) == [
        "a.py has 3 type errors, more than the 2 the base allows",
        f"{type_gate.BASELINE} records 2 type errors in a.py, the head has 3; run python -m scripts.type_gate --write",
    ]


def test_grade_holds_growth_only_on_touched_files() -> None:
    assert type_gate.grade({"a.py": 2}, {"a.py": 3}, {"a.py": 2}, set()) == []
    assert type_gate.grade({}, {"a.py": 169}, {}, {"b.py"}) == []


def test_grade_reports_only_growth_for_a_new_file_absent_from_both_baselines() -> None:
    assert type_gate.grade({}, {"a.py": 1}, {}, {"a.py"}) == [
        "a.py has 1 type errors, more than the 0 the base allows",
        f"{type_gate.BASELINE} records 0 type errors in a.py, the head has 1; run python -m scripts.type_gate --write",
    ]


def test_a_baseline_merged_without_another_file_s_errors_does_not_freeze_the_next_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _tree(tmp_path / "base", {"hooks/service.py": ILL_TYPED}, {})
    head = _tree(tmp_path / "head", {"hooks/service.py": ILL_TYPED, "hooks/other.py": WELL_TYPED}, {})
    assert _grade(base, head, capsys) == (0, "0 type errors in 0 files, 0 allowed, 0 errors\n")


def test_a_baseline_refresh_matching_the_measurement_is_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _tree(tmp_path / "base", {"hooks/service.py": ILL_TYPED}, {})
    head = _tree(tmp_path / "head", {"hooks/service.py": ILL_TYPED}, {"hooks/service.py": 1})
    assert _grade(base, head, capsys) == (0, "1 type errors in 1 files, 0 allowed, 0 errors\n")


def test_a_refresh_raising_an_entry_past_the_measurement_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _tree(tmp_path / "base", {"hooks/service.py": ILL_TYPED}, {})
    head = _tree(tmp_path / "head", {"hooks/service.py": ILL_TYPED}, {"hooks/service.py": 5})
    assert _grade(base, head, capsys) == (
        1,
        "1 type errors in 1 files, 0 allowed, 1 errors\n"
        f"::error::{type_gate.BASELINE} records 5 type errors in hooks/service.py, the head has 1; "
        "run python -m scripts.type_gate --write\n",
    )


def test_a_head_that_touches_a_file_over_a_stale_baseline_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _tree(tmp_path / "base", {"hooks/service.py": ILL_TYPED}, {})
    head = _tree(tmp_path / "head", {"hooks/service.py": ILL_TYPED + "\nx = 1\n"}, {"hooks/service.py": 1})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "::error::hooks/service.py has 1 type errors, more than the 0 the base allows\n" in out


def test_a_base_without_a_baseline_grades_drift_on_every_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _tree(tmp_path / "base", {"hooks/planted.py": ILL_TYPED})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 2})
    assert _grade(base, head, capsys) == (
        1,
        f"::notice::The base carries no {type_gate.BASELINE}; this head introduces it.\n"
        "1 type errors in 1 files, 2 allowed, 1 errors\n"
        f"::error::{type_gate.BASELINE} records 2 type errors in hooks/planted.py, the head has 1; "
        "run python -m scripts.type_gate --write\n",
    )


def test_a_head_that_drops_the_baseline_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 1})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED})
    assert _grade(base, head, capsys) == (
        1,
        "1 type errors in 1 files, 1 allowed, 1 errors\n"
        f"::error::{type_gate.BASELINE} records 0 type errors in hooks/planted.py, the head has 1; "
        "run python -m scripts.type_gate --write\n",
    )


def test_changed_names_files_whose_contents_differ(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", {"hooks/same.py": "x = 1\n", "hooks/edited.py": "x = 1\n"})
    head = _tree(tmp_path / "head", {"hooks/same.py": "x = 1\n", "hooks/edited.py": "x = 2\n", "hooks/new.py": ""})
    paths = {"hooks/same.py", "hooks/edited.py", "hooks/new.py", "hooks/gone.py"}
    assert type_gate.changed(base, head, paths) == {"hooks/edited.py", "hooks/new.py"}


def test_a_changed_signature_reds_an_untouched_importer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lib = "def size(value: int) -> int:\n    return value\n"
    user = "from hooks.lib import size\n\n\ndef total() -> int:\n    return size(1)\n"
    files = {"hooks/user.py": user, "hooks/other.py": ILL_TYPED}
    base = _tree(tmp_path / "base", {**files, "hooks/lib.py": lib}, {})
    head = _tree(
        tmp_path / "head",
        {**files, "hooks/lib.py": lib.replace("value: int", "value: str").replace("value\n", "len(value)\n")},
        {},
    )
    assert _grade(base, head, capsys) == (
        1,
        "1 type errors in 1 files, 0 allowed, 2 errors\n"
        "::error::hooks/user.py has 1 type errors, more than the 0 the base allows\n"
        f"::error::{type_gate.BASELINE} records 0 type errors in hooks/user.py, the head has 1; "
        "run python -m scripts.type_gate --write\n",
    )


@pytest.mark.parametrize(
    "path,module",
    [("hooks/a/b.py", "hooks.a.b"), ("hooks/a/__init__.py", "hooks.a"), ("scripts/c.pyi", "scripts.c")],
)
def test_module_name_maps_a_source_to_its_module(path: str, module: str) -> None:
    assert type_gate.module_name(path) == module


def test_imported_resolves_absolute_relative_and_nested_imports() -> None:
    source = (
        b"import os.path\nfrom . import sibling\nfrom .. import top\nfrom .inner import name\n"
        b"from hooks.lib import size\nimport hooks.alias.deep as deep\n\n\ndef late() -> None:\n    import json\n"
    )
    assert type_gate.imported("hooks/pkg/mod.py", source) == {
        "os",
        "os.path",
        "hooks",
        "hooks.pkg",
        "hooks.pkg.sibling",
        "hooks.top",
        "hooks.pkg.inner",
        "hooks.pkg.inner.name",
        "hooks.lib",
        "hooks.lib.size",
        "hooks.alias",
        "hooks.alias.deep",
        "json",
    }
    assert type_gate.imported("hooks/pkg/__init__.py", b"from . import sibling\nfrom .sub import x\n") == {
        "hooks",
        "hooks.pkg",
        "hooks.pkg.sibling",
        "hooks.pkg.sub",
        "hooks.pkg.sub.x",
    }


def test_importers_follow_imports_through_cycles_and_skip_unparsable_sources(tmp_path: Path) -> None:
    head = _tree(
        tmp_path,
        {
            "hooks/lib.py": "",
            "hooks/user.py": "from .lib import size\n",
            "hooks/far.py": "from hooks import user\n",
            "hooks/loop_a.py": "from hooks import loop_b, lib\n",
            "hooks/loop_b.py": "from hooks import loop_a\n",
            "hooks/other.py": "import os\n",
            "hooks/broken.py": "def (\n",
        },
    )
    assert type_gate.importers(head, type_gate.sources(head), {"hooks.lib"}) == {
        "hooks/user.py",
        "hooks/far.py",
        "hooks/loop_a.py",
        "hooks/loop_b.py",
    }


def test_graded_selects_changes_importers_and_edited_baseline_entries(tmp_path: Path) -> None:
    same = {
        "hooks/user.py": "from hooks import lib\n",
        "hooks/legacy.py": "from hooks.old import gone\n",
        "hooks/still.py": "",
        "hooks/zero.py": "",
    }
    base = _tree(tmp_path / "base", {**same, "hooks/lib.py": "x = 1\n", "hooks/old.py": ""})
    head = _tree(tmp_path / "head", {**same, "hooks/lib.py": "x = 2\n", "hooks/new.py": ""})
    base_baseline = {"hooks/still.py": 2, "hooks/x.py": 1}
    recorded = {"hooks/still.py": 1, "hooks/x.py": 1, "hooks/zero.py": 0}
    assert type_gate.graded(base, head) == {
        "hooks/lib.py",
        "hooks/user.py",
        "hooks/legacy.py",
        "hooks/old.py",
        "hooks/new.py",
    }
    assert type_gate.edited(base_baseline, recorded) == {"hooks/still.py"}
    assert type_gate.edited({"hooks/gone.py": 0}, {}) == set()


def _fake_checker(
    monkeypatch: pytest.MonkeyPatch, diagnostics: list[dict[str, str]], files: int = 1
) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"basedpyright {type_gate.BASEDPYRIGHT_VERSION}\n", "")
        commands.append(command)
        report = {"generalDiagnostics": diagnostics, "summary": {"filesAnalyzed": files}}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    return commands


def test_a_head_without_source_changes_runs_no_checker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _tree(tmp_path / "base", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 1})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED, "docs/notes.md": "new\n"}, {"hooks/planted.py": 1})
    commands = _fake_checker(monkeypatch, [], 0)
    measured = tmp_path / "out" / "TYPE_BASELINE.json"
    assert type_gate.main(["--base", str(base), "--head", str(head), "--measured", str(measured)]) == 0
    assert capsys.readouterr().out == "0 type errors in 0 files, 1 allowed, 0 errors\n"
    assert commands == []
    assert measured.read_text() == '{\n  "hooks/planted.py": 1\n}\n'


def test_the_measurement_checks_only_graded_sources_and_keeps_other_recorded_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = {"hooks/a.py": 1, "hooks/z.py": 2}
    base = _tree(tmp_path / "base", {"hooks/z.py": ILL_TYPED}, baseline)
    head = _tree(tmp_path / "head", {"hooks/z.py": ILL_TYPED, "hooks/a.py": ILL_TYPED}, baseline)
    commands = _fake_checker(monkeypatch, [{"file": str(head / "hooks/a.py"), "severity": "error"}])
    measured = tmp_path / "out" / "TYPE_BASELINE.json"
    assert type_gate.main(["--base", str(base), "--head", str(head), "--measured", str(measured)]) == 0
    assert capsys.readouterr().out == "1 type errors in 1 files, 3 allowed, 0 errors\n"
    assert [command[-1] for command in commands] == ["hooks/a.py"]
    assert measured.read_text() == '{\n  "hooks/a.py": 1,\n  "hooks/z.py": 2\n}\n'


def test_a_base_without_a_baseline_measures_and_records_every_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _tree(tmp_path / "base", {"hooks/a.py": ""})
    head = _tree(tmp_path / "head", {"hooks/a.py": ""}, {"hooks/a.py": 1, "hooks/gone.py": 3})
    commands = _fake_checker(monkeypatch, [{"file": str(head / "hooks/a.py"), "severity": "error"}])
    measured = tmp_path / "out" / "TYPE_BASELINE.json"
    assert type_gate.main(["--base", str(base), "--head", str(head), "--measured", str(measured)]) == 1
    assert [command[-2:] for command in commands] == [["hooks/__init__.py", "hooks/a.py"]]
    assert measured.read_text() == '{\n  "hooks/a.py": 1\n}\n'
    assert capsys.readouterr().out.endswith(
        f"::error::{type_gate.BASELINE} records 3 type errors in hooks/gone.py, the head has 0; "
        "run python -m scripts.type_gate --write\n"
    )


def test_drift_on_an_untouched_file_stays_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 2})
    head = _tree(tmp_path / "head", {"hooks/planted.py": ILL_TYPED}, {"hooks/planted.py": 2})
    assert _grade(base, head, capsys) == (0, "0 type errors in 0 files, 2 allowed, 0 errors\n")


def test_grade_treats_a_zero_record_as_no_errors() -> None:
    assert type_gate.grade({}, {}, {"a.py": 0}) == []
    assert type_gate.grade({"a.py": 0}, {"a.py": 0}, {}) == []


def test_a_pyproject_without_a_tool_table_is_red(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    assert type_gate.config_errors(tmp_path) == [
        f"pyproject.toml [tool.basedpyright] is None; the type gate grades only with {type_gate.CONFIG!r}"
    ]


def test_measure_counts_every_error_in_a_file(tmp_path: Path) -> None:
    head = _tree(tmp_path, {"hooks/planted.py": ILL_TYPED + ILL_TYPED.replace("planted", "other")})
    assert type_gate.measure(head) == {"hooks/planted.py": 2}


def test_write_records_an_indented_baseline(tmp_path: Path) -> None:
    head = _tree(tmp_path, {"hooks/planted.py": ILL_TYPED}, {})
    assert type_gate.main(["--write", "--head", str(head)]) == 0
    assert (head / type_gate.BASELINE).read_text() == '{\n  "hooks/planted.py": 1\n}\n'


def test_a_checker_failure_names_its_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _tree(tmp_path / "base", {}, {})
    head = _tree(tmp_path / "head", {"hooks/planted.py": WELL_TYPED}, {})
    calls: list[dict[str, object]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"basedpyright {type_gate.BASEDPYRIGHT_VERSION}\n", "")
        return subprocess.CompletedProcess(command, 2, "", "boom\n")

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    assert _grade(base, head, capsys) == (1, "::error::basedpyright exited 2: boom\n")
    assert calls[-1] == {
        "cwd": head,
        "env": {**os.environ, "NODE_OPTIONS": "--max-old-space-size=6656"},
        "capture_output": True,
        "text": True,
    }


def test_sources_lists_checked_files_in_sorted_order(tmp_path: Path) -> None:
    files = {
        "scripts/z.py": "",
        "scripts/a/b.pyi": "",
        "tests/m.py": "",
        "hooks/notes.pyc": "",
        "hooks/__pycache__/c.py": "",
        "hooks/.hidden/d.py": "",
        "docs/e.py": "",
    }
    head = _tree(tmp_path, files)
    assert type_gate.sources(head) == ["hooks/__init__.py", "scripts/a/b.pyi", "scripts/z.py", "tests/m.py"]


def test_measure_checks_the_sorted_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    head = _tree(tmp_path, {"scripts/b.py": "", "scripts/a.py": ""})
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, '{"generalDiagnostics": [], "summary": {"filesAnalyzed": 3}}', ""
        )

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    assert type_gate.measure(head) == {}
    assert commands == [
        [
            *type_gate._BASEDPYRIGHT,
            "--outputjson",
            "--project",
            str(head / "pyproject.toml"),
            "--pythonpath",
            type_gate.sys.executable,
            "hooks/__init__.py",
            "scripts/a.py",
            "scripts/b.py",
        ]
    ]


@pytest.mark.parametrize(
    "returncode,stdout,message",
    [
        (1, "not json", "basedpyright printed no report: crash"),
        (0, '{"generalDiagnostics": [], "summary": {"filesAnalyzed": 0}}', "basedpyright analysed no files"),
    ],
)
def test_a_checker_run_without_a_usable_report_is_red(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    base = _tree(tmp_path / "base", {}, {})
    head = _tree(tmp_path / "head", {"hooks/planted.py": WELL_TYPED}, {})

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"basedpyright {type_gate.BASEDPYRIGHT_VERSION}\n", "")
        return subprocess.CompletedProcess(command, returncode, stdout, "crash\n")

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    assert _grade(base, head, capsys) == (1, f"::error::{message}\n")


def test_the_parser_reads_paths_and_defaults_the_head_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    args = type_gate.build_parser().parse_args(["--base", "b"])
    assert (args.base, args.head) == (Path("b"), Path.cwd())
