import json
import subprocess
import threading
from pathlib import Path

import pytest

from scripts import type_gate
from tests.type_gate_support import record_checks, source_tree

pytestmark = pytest.mark.unit


@pytest.mark.feature("type-gate-repeatable-counts")
def test_measure_checks_each_run_in_one_process_and_sums_their_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = source_tree(tmp_path, 1000)
    runs = record_checks(monkeypatch, tmp_path)
    assert type_gate.measure(tmp_path, files[::-1]) == dict.fromkeys(files, 1)
    assert [len(checked) for checked in sorted(runs)] == [120, 4, 213, 110, 224, 228, 74, 27]
    assert [path for checked in sorted(runs) for path in checked] == files


@pytest.mark.feature("type-gate-one-context")
def test_adding_or_removing_a_source_changes_only_the_run_it_falls_in() -> None:
    files = [f"tests/test_{index:04}.py" for index in range(1000)]
    before = type_gate.runs(files)
    for changed in (sorted([*files, "tests/test_0500a.py"]), files[:500] + files[501:]):
        after = type_gate.runs(changed)
        assert len([run for run in after if run not in before]) == 1
        assert len([run for run in before if run not in after]) == 1


@pytest.mark.feature("type-gate-one-context")
def test_the_gate_checks_a_run_of_the_every_source_layout_through_its_last_graded_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = source_tree(tmp_path, 300)
    runs = record_checks(monkeypatch, tmp_path)
    graded = [files[250], files[30], files[200]]
    assert type_gate.measure(tmp_path, graded) == dict.fromkeys(sorted(graded), 1)
    assert sorted(runs) == [files[:31], files[124:251]]


@pytest.mark.feature("type-gate-one-context")
def test_the_gate_checks_four_runs_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = source_tree(tmp_path, 2000)
    barrier = threading.Barrier(4, timeout=5)
    lock = threading.Lock()
    active = peak = calls = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            first = calls <= 4
        if first:
            barrier.wait()
        with lock:
            active -= 1
        report = {"generalDiagnostics": [], "summary": {"filesAnalyzed": 1}}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    monkeypatch.setattr(type_gate.subprocess, "run", run)
    assert type_gate.measure(tmp_path) == {}
    assert (peak, calls) == (4, len(type_gate.runs(files)))
