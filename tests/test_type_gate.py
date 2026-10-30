import json
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


def test_the_baseline_is_registered_shrink_only() -> None:
    from scripts import threshold_pins

    assert str(type_gate.BASELINE) in threshold_pins.SHRINK_ONLY


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
    measured = tmp_path / "out" / "TYPE_BASELINE.json"
    assert type_gate.main(["--base", str(base), "--head", str(head), "--measured", str(measured)]) == 0
    assert json.loads(measured.read_text()) == {"hooks/planted.py": 1}
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
    head = _tree(tmp_path, {}, {})
    monkeypatch.setattr(type_gate, "_basedpyright_version", lambda: type_gate.BASEDPYRIGHT_VERSION)
    monkeypatch.setattr(type_gate, "_BASEDPYRIGHT", (*type_gate._BASEDPYRIGHT, "--no-such-flag"))
    code, out = _grade(head, head, capsys)
    assert code == 1
    assert out.startswith("::error::basedpyright exited ")


def test_grading_needs_a_base(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        type_gate.main([])
    assert exited.value.code == 2
    assert "grading needs --base" in capsys.readouterr().err


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
