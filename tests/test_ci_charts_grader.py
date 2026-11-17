import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _graders(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    monkeypatch.syspath_prepend(str(ROOT / "tests/ci_charts"))
    for name in ("chart_timers", "chart_pod_templates"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("chart_timers"), importlib.import_module("chart_pod_templates")


def _chart(root: Path, required: bool) -> Path:
    chart = root / "deploy" / "chart"
    (chart / "ci").mkdir(parents=True)
    for name in ("Chart.yaml", "values.yaml", "ci/kind.yaml"):
        (chart / name).write_text("{}\n")
    if required:
        (chart / "ci/required-values.yaml").write_text("{}\n")
    return chart


def _recorded(monkeypatch: pytest.MonkeyPatch, timers: ModuleType) -> list[list[str]]:
    seen: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(timers, "subprocess", SimpleNamespace(run=run))
    return seen


def _commands(monkeypatch: pytest.MonkeyPatch, chart: Path) -> list[list[str]]:
    timers, _ = _graders(monkeypatch)
    seen = _recorded(monkeypatch, timers)
    for values in timers.values_files(chart):
        timers.render(chart, values)
    return seen


def test_a_chart_with_required_values_renders_every_values_file_over_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _chart(tmp_path, required=True)
    required = str(chart / "ci/required-values.yaml")

    assert _commands(monkeypatch, chart) == [
        ["helm", "template", "timer-check", str(chart), "-f", required],
        ["helm", "template", "timer-check", str(chart), "-f", required, "-f", str(chart / "ci/kind.yaml")],
    ]


def test_a_chart_without_required_values_renders_its_bare_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _chart(tmp_path, required=False)

    assert _commands(monkeypatch, chart) == [
        ["helm", "template", "timer-check", str(chart)],
        ["helm", "template", "timer-check", str(chart), "-f", str(chart / "ci/kind.yaml")],
    ]


def test_the_pod_template_grader_renders_the_chart_and_its_bumped_copy_over_their_required_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timers, pods = _graders(monkeypatch)
    chart = _chart(tmp_path, required=True)
    seen = _recorded(monkeypatch, timers)

    pods.main(["--root", str(tmp_path)])

    assert len(seen) == 4
    assert len({command[3] for command in seen}) == 2
    for command in seen:
        assert command[4:6] == ["-f", str(Path(command[3]) / "ci/required-values.yaml")]
        assert command[6:] in ([], ["-f", str(chart / "ci/kind.yaml")])
