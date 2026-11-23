import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import private_test_imports as gate

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_HEADER = '"""Café €."""\n'
_FLAGS = ["--metrics", "off", "--json", "--strict", "--timeout", "30", "--x-ignore-semgrepignore-files"]


def _finding(path: str, text: str, name: str) -> dict[str, Any]:
    start = text.encode().index(name.encode())
    return {"path": path, "start": {"offset": start}, "end": {"offset": start + len(name.encode())}}


def _module(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)


def _listed(root: Path, entries: list[str]) -> None:
    (root / gate.ALLOWLIST).parent.mkdir(parents=True, exist_ok=True)
    (root / gate.ALLOWLIST).write_text(json.dumps(entries))


@pytest.mark.feature("test-private-imports-ratchet")
def test_offenders_key_each_finding_by_its_file_and_the_name_its_byte_span_covers(tmp_path: Path) -> None:
    text = _HEADER + "from tests.b import public, _helper\nfrom tests.c import (\n    _other,\n)\n"
    _module(tmp_path, "tests/test_a.py", text)
    report = {
        "results": [
            _finding("tests/test_a.py", text, "_helper"),
            _finding("tests/test_a.py", text, "_other"),
            _finding("tests/test_a.py", text, "_helper"),
        ]
    }
    assert gate.offenders(report, tmp_path) == {"tests/test_a.py::_helper", "tests/test_a.py::_other"}


@pytest.mark.feature("test-private-imports-ratchet")
def test_grade_refuses_a_new_offender_the_list_does_not_name() -> None:
    kept = "tests/test_a.py::_kept"
    assert gate.grade({kept, "tests/test_b.py::_new"}, {kept}, {kept}, {kept}) == [
        "tests/test_b.py imports the private name _new from another test module; "
        "make the helper public in a shared test module instead"
    ]


@pytest.mark.feature("test-private-imports-ratchet")
def test_grade_passes_offenders_the_list_names() -> None:
    kept = {"tests/test_a.py::_kept", "tests/test_b.py::_moved"}
    assert gate.grade({"tests/test_b.py::_moved"}, kept, kept, kept) == []


@pytest.mark.feature("test-private-imports-ratchet")
def test_grade_refuses_a_listed_entry_its_file_no_longer_imports() -> None:
    listed = {"tests/test_a.py::_kept", "tests/test_a.py::_gone"}
    assert gate.grade(set(), {"tests/test_a.py::_kept"}, listed, listed) == [
        f"{gate.ALLOWLIST} lists tests/test_a.py::_gone, which its file no longer imports; "
        "run python -m scripts.private_test_imports write"
    ]


@pytest.mark.feature("test-private-imports-ratchet")
def test_grade_passes_a_stale_entry_in_a_file_the_change_did_not_touch() -> None:
    assert gate.grade(set(), set(), {"tests/test_a.py::_gone"}, set()) == []


@pytest.mark.feature("test-private-imports-ratchet")
def test_changed_lists_the_files_between_the_baseline_and_head_deleted_ones_included(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    _module(tmp_path, "tests/test_a.py", "a = 1\n")
    _module(tmp_path, "tests/test_b.py", "b = 1\n")
    _module(tmp_path, "tests/test_c.py", "c = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    baseline = git("rev-parse", "HEAD").strip()
    _module(tmp_path, "tests/test_a.py", "a = 2\n")
    (tmp_path / "tests/test_b.py").unlink()
    git("add", "-A")
    git("commit", "-q", "-m", "head")
    assert gate.changed(baseline, tmp_path) == {Path("tests/test_a.py"), Path("tests/test_b.py")}
    with pytest.raises(SystemExit, match=r"^git diff exited 128: "):
        gate.changed("0" * 40, tmp_path)


@pytest.mark.feature("test-private-imports-ratchet")
def test_configs_add_the_grader_copy_only_when_the_grader_holds_the_rule(tmp_path: Path) -> None:
    assert gate.configs(None) == [gate.RULE]
    assert gate.configs(tmp_path) == [gate.RULE]
    _module(tmp_path, str(gate.RULE), "rules: []\n")
    assert gate.configs(tmp_path) == [gate.RULE, tmp_path / gate.RULE]


@pytest.mark.feature("test-private-imports-ratchet")
def test_scan_runs_strict_json_semgrep_past_the_ignore_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='{"results": [1]}', stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert gate.scan([Path("a.yml"), Path("b.yml")], [Path("tests")], "abc") == {"results": [1]}
    assert gate.scan([Path("a.yml")], [Path("tests/test_a.py"), Path("tests/test_b.py")]) == {"results": [1]}
    options = {"capture_output": True, "text": True, "check": False}
    assert calls == [
        (
            ["semgrep", "scan", *_FLAGS, "--config", "a.yml", "--config", "b.yml", "--baseline-commit", "abc", "tests"],
            options,
        ),
        (["semgrep", "scan", *_FLAGS, "--config", "a.yml", "tests/test_a.py", "tests/test_b.py"], options),
    ]


@pytest.mark.feature("test-private-imports-ratchet")
def test_scan_refuses_a_semgrep_run_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr=" rule failed to parse \n")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit, match=r"^semgrep exited 2: rule failed to parse$"):
        gate.scan([gate.RULE], [gate.TESTS])


@pytest.mark.feature("test-private-imports-ratchet")
def test_check_grades_new_findings_and_rescans_only_the_listed_files_the_change_touches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kept = _HEADER + "from tests.b import _kept\n"
    new = "from tests.c import _new\n"
    _module(tmp_path, "tests/test_a.py", kept)
    _module(tmp_path, "tests/test_b.py", new)
    _module(tmp_path, "tests/test_other.py", "from tests.d import _untouched\n")
    _listed(tmp_path, ["tests/test_a.py::_kept", "tests/test_gone.py::_lost", "tests/test_other.py::_untouched"])
    calls: list[tuple[list[Path], list[Path], str | None]] = []

    def scan(rules: list[Path], targets: list[Path], baseline: str | None = None) -> dict[str, Any]:
        calls.append((rules, targets, baseline))
        if baseline is None:
            return {"results": [_finding("tests/test_a.py", kept, "_kept")]}
        return {"results": [_finding("tests/test_b.py", new, "_new"), _finding("tests/test_a.py", kept, "_kept")]}

    touched = {Path("tests/test_a.py"), Path("tests/test_b.py"), Path("tests/test_gone.py")}
    monkeypatch.setattr(gate, "scan", scan)
    monkeypatch.setattr(
        gate, "changed", lambda baseline, root: touched if (baseline, root) == ("abc", tmp_path) else set()
    )
    assert gate.check("abc", tmp_path / "grader", tmp_path) == 1
    assert calls == [([gate.RULE], [gate.TESTS], "abc"), ([gate.RULE], [Path("tests/test_a.py")], None)]
    assert capsys.readouterr().out.splitlines() == [
        "::error::tests/test_b.py imports the private name _new from another test module; "
        "make the helper public in a shared test module instead",
        f"::error::{gate.ALLOWLIST} lists tests/test_gone.py::_lost, which its file no longer imports; "
        "run python -m scripts.private_test_imports write",
        "3 listed private test imports, 1 new, 1 no longer imported",
    ]


@pytest.mark.feature("test-private-imports-ratchet")
def test_check_passes_when_the_list_names_every_offender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kept = "from tests.b import _kept\n"
    _module(tmp_path, "tests/test_a.py", kept)
    _listed(tmp_path, ["tests/test_a.py::_kept"])
    _module(tmp_path, f"grader/{gate.RULE}", "rules: []\n")
    rules = [gate.RULE, tmp_path / "grader" / gate.RULE]
    calls: list[str | None] = []

    def scan(given: list[Path], targets: list[Path], baseline: str | None = None) -> dict[str, Any]:
        assert given == rules
        calls.append(baseline)
        return {"results": [_finding("tests/test_a.py", kept, "_kept")]}

    monkeypatch.setattr(gate, "scan", scan)
    monkeypatch.setattr(gate, "changed", lambda baseline, root: {Path("tests/test_a.py")})
    assert gate.check("abc", tmp_path / "grader", tmp_path) == 0
    assert calls == ["abc", None]
    assert capsys.readouterr().out == "1 listed private test imports, 0 new, 0 no longer imported\n"


@pytest.mark.feature("test-private-imports-ratchet")
def test_check_skips_the_listed_scan_when_the_change_touches_no_listed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _module(tmp_path, "tests/test_a.py", "from tests.b import _kept\n")
    _listed(tmp_path, ["tests/test_a.py::_kept"])
    calls: list[str | None] = []

    def scan(rules: list[Path], targets: list[Path], baseline: str | None = None) -> dict[str, Any]:
        calls.append(baseline)
        return {"results": []}

    monkeypatch.setattr(gate, "scan", scan)
    monkeypatch.setattr(gate, "changed", lambda baseline, root: {Path("tests/test_b.py")})
    assert gate.check("abc", None, tmp_path) == 0
    assert calls == ["abc"]
    assert capsys.readouterr().out == "1 listed private test imports, 0 new, 0 no longer imported\n"


@pytest.mark.feature("test-private-imports-ratchet")
def test_write_records_every_offender_sorted_and_indented_by_one_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = "from tests.c import _b, _a\n"
    second = "from tests.c import _c\n"
    _module(tmp_path, "tests/test_b.py", first)
    _module(tmp_path, "tests/test_a.py", second)
    _listed(tmp_path, ["tests/test_old.py::_old"])
    calls: list[tuple[list[Path], list[Path], str | None]] = []

    def scan(rules: list[Path], targets: list[Path], baseline: str | None = None) -> dict[str, Any]:
        calls.append((rules, targets, baseline))
        found = [_finding("tests/test_b.py", first, "_b"), _finding("tests/test_b.py", first, "_a")]
        return {"results": [*found, _finding("tests/test_a.py", second, "_c")]}

    monkeypatch.setattr(gate, "scan", scan)
    assert gate.write(tmp_path) == 0
    assert calls == [([gate.RULE], [gate.TESTS], None)]
    assert (tmp_path / gate.ALLOWLIST).read_text() == (
        '[\n "tests/test_a.py::_c",\n "tests/test_b.py::_a",\n "tests/test_b.py::_b"\n]\n'
    )
    assert capsys.readouterr().out == f"{gate.ALLOWLIST} lists 3 private test imports\n"


@pytest.mark.feature("test-private-imports-ratchet")
def test_main_routes_check_and_write_from_the_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gate, "check", lambda baseline, grader, root: calls.append((baseline, grader, root)) or 1)
    monkeypatch.setattr(gate, "write", lambda root: calls.append((root,)) or 0)
    assert gate.main(["check", "--baseline-commit", "abc", "--grader", "/grader"]) == 1
    assert gate.main(["check", "--baseline-commit", "abc"]) == 1
    assert gate.main(["write"]) == 0
    assert calls == [("abc", Path("/grader"), Path.cwd()), ("abc", None, Path.cwd()), (Path.cwd(),)]
    with pytest.raises(SystemExit):
        gate.main(["check"])


@pytest.mark.feature("test-private-imports-ratchet")
def test_the_list_names_each_offender_once_in_order_with_an_existing_test_file() -> None:
    entries = json.loads((_ROOT / gate.ALLOWLIST).read_text())
    assert entries
    assert entries == sorted(set(entries))
    for entry in entries:
        assert re.fullmatch(r"tests/[\w/]+\.py::_\w+", entry), entry
        assert (_ROOT / entry.split("::")[0]).is_file(), entry


@pytest.mark.feature("test-private-imports-ratchet")
def test_the_rule_reads_only_test_modules_and_its_fixture() -> None:
    (rule,) = yaml.safe_load((_ROOT / gate.RULE).read_text())["rules"]
    fixture = (_ROOT / gate.RULE).with_suffix(".test.py")
    assert rule["id"] == "test-private-imports"
    assert rule["paths"]["include"] == ["/tests/", fixture.name]
    assert {"focus-metavariable": "$NAME"} in rule["patterns"]
    text = fixture.read_text()
    assert "# ruleid: test-private-imports" in text
    assert "# ok: test-private-imports" in text
