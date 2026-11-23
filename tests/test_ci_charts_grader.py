import importlib.util
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/ci_charts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _graders(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    timers = _load("chart_timers")
    monkeypatch.setitem(sys.modules, "chart_timers", timers)
    return timers, _load("chart_pod_templates")


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


def _sealed(tree: Path) -> None:
    for path in (tree, *tree.rglob("*")):
        path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


@pytest.mark.feature("chart-grader-sealed-head")
def test_the_bumped_copy_of_a_read_only_chart_carries_the_probe_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, pods = _graders(monkeypatch)
    chart = _chart(tmp_path / "head", required=True)
    (chart / "Chart.yaml").write_text("name: chart\nversion: 1.2.3\n")
    _sealed(chart)

    copy = pods.bumped_copy(chart, tmp_path / "scratch")

    bumped = copy / "Chart.yaml"
    assert yaml.safe_load(bumped.read_text()) == {"name": "chart", "version": pods.PROBE_VERSION}
    assert bumped.stat().st_mode & stat.S_IWUSR
    assert (copy / "ci/required-values.yaml").read_text() == "{}\n"
    assert (chart / "Chart.yaml").read_text() == "name: chart\nversion: 1.2.3\n"
    assert not (chart / "Chart.yaml").stat().st_mode & stat.S_IWUSR
