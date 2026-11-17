import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _timers() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chart_timers", ROOT / "tests/ci_charts/chart_timers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chart(root: Path, required: bool) -> Path:
    chart = root / "deploy" / "chart"
    (chart / "ci").mkdir(parents=True)
    for name in ("Chart.yaml", "values.yaml", "ci/kind.yaml"):
        (chart / name).write_text("{}\n")
    if required:
        (chart / "ci/required-values.yaml").write_text("{}\n")
    return chart


def _commands(monkeypatch: pytest.MonkeyPatch, chart: Path) -> list[list[str]]:
    timers = _timers()
    seen: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(timers.subprocess, "run", run)
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
