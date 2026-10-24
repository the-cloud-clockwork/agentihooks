import gc
import os
import sys
import threading
import time
import tracemalloc
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2 import resource_sampling as sampling

pytestmark = pytest.mark.unit

WORKLOADS = Path(__file__).parent / "performance" / "swarm_v2" / "workloads"
COMM = "w (x) y"
PGRP = 999
TRACE_FRAMES = 32


def killed(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def proc_entry(proc: Path, pid: int, ppid: int, session: int, ticks=(0, 0), pages=0, pss_kb=None):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    rest = ["S", ppid, PGRP, session, 0, -1, 4194304, 0, 0, 0, 0, ticks[0], ticks[1], 0, 0, 20, 0, 1, 0, 100]
    (entry / "stat").write_text(f"{pid} ({COMM}) " + " ".join(str(v) for v in rest) + "\n")
    (entry / "statm").write_text(f"1000 {pages} 10 1 0 50 0\n")
    if pss_kb is not None:
        (entry / "smaps_rollup").write_text(
            f"00400000-ffff [rollup]\nRss: 999 kB\nPss: {pss_kb} kB\nShared_Clean: 1 kB\n"
        )


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "self").mkdir()
    (root / "meminfo").write_text("MemTotal: 1 kB\n")
    return root


def test_read_parses_a_process_whose_name_holds_parentheses(proc):
    proc_entry(proc, 40, 7, 40, ticks=(30, 12), pages=5, pss_kb=8)
    assert sampling.read(proc, 40) == sampling.Process(40, 7, 40, 42, 5 * sampling.PAGE)


def test_pss_reads_smaps_rollup_and_falls_back_to_rss(proc):
    proc_entry(proc, 40, 7, 40, pages=5, pss_kb=8)
    proc_entry(proc, 41, 7, 40, pages=3)
    proc_entry(proc, 45, 7, 40, pages=2)
    (proc / "45" / "smaps_rollup").write_text("Rss: 8 kB\n")
    assert [sampling.pss(proc, sampling.read(proc, pid)) for pid in (40, 41, 45)] == [
        8 * 1024,
        3 * sampling.PAGE,
        2 * sampling.PAGE,
    ]


def test_read_treats_a_vanished_process_as_gone(proc):
    (proc / "42").mkdir()
    assert sampling.read(proc, 42) is None
    assert sampling.read(proc, 43) is None


def test_read_treats_a_reaped_process_as_gone(proc, monkeypatch):
    proc_entry(proc, 44, 7, 40)
    real = Path.read_text

    def reaped(path, *args, **kwargs):
        if path.name == "statm":
            raise ProcessLookupError(3, "No such process")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reaped)
    assert sampling.read(proc, 44) is None


def test_table_lists_only_numeric_process_entries(proc):
    proc_entry(proc, 50, 1, 50)
    proc_entry(proc, 51, 50, 50)
    assert sorted(sampling.table(proc)) == [50, 51]


def test_members_follow_children_and_the_root_session_only(proc):
    proc_entry(proc, 10, 1, 10)
    proc_entry(proc, 11, 10, 10)
    proc_entry(proc, 12, 11, 12)
    proc_entry(proc, 16, 12, 12)
    proc_entry(proc, 13, 1, 10)
    proc_entry(proc, 14, 1, 14)
    proc_entry(proc, 15, 14, 14)
    members = sampling.members(sampling.table(proc), 10)
    assert [p.pid for p in members] == [10, 11, 12, 13, 16]


def test_members_of_a_gone_root_are_empty(proc):
    proc_entry(proc, 14, 1, 14)
    assert sampling.members(sampling.table(proc), 10) == []


def test_snapshot_sums_the_tree_and_keeps_the_root_apart(proc):
    proc_entry(proc, 10, 1, 10, ticks=(5, 5), pages=2, pss_kb=4)
    proc_entry(proc, 11, 10, 10, ticks=(1, 2), pages=3)
    snap = sampling.snapshot(proc, 10, at=2.5)
    pages = sampling.PAGE
    assert snap == sampling.Snapshot(2.5, 2, 5 * pages, 4 * 1024 + 3 * pages, 2 * pages, {10: 10, 11: 3}, 10)
    assert sampling.snapshot(proc, 99, at=1.0) == sampling.Snapshot(1.0, 0, 0, 0, 0, {}, 99)


def test_peaks_keep_maxima_and_count_cores_of_children_that_exited_inside_a_window():
    t = sampling.TICKS
    peaks = sampling.Peaks()
    peaks.add(sampling.Snapshot(0.0, 2, 100, 80, 60, {1: 10, 2: 10}, 1), 5)
    peaks.add(sampling.Snapshot(0.5, 3, 300, 90, 50, {1: 10 + t, 2: 10, 3: t // 2}, 1), 9)
    assert (peaks.cores, peaks.parent_cores, peaks.children_cores) == (0.0, 0.0, 0.0)
    peaks.add(sampling.Snapshot(1.0, 2, 200, 85, 70, {1: 10 + 2 * t, 2: 10 + t}, 1), 7)
    assert peaks.cores == pytest.approx(3.5)
    assert peaks.parent_cores == pytest.approx(2.0)
    assert peaks.children_cores == pytest.approx(1.5)
    peaks.add(sampling.Snapshot(2.5, 1, 50, 40, 50, {1: 10 + 3 * t}, 1), 1)
    assert (peaks.processes, peaks.rss_bytes, peaks.pss_bytes, peaks.storage_bytes) == (3, 300, 90, 9)
    assert peaks.children_processes == 2
    assert (peaks.parent_rss_bytes, peaks.children_rss_bytes) == (70, 250)
    assert peaks.cores == pytest.approx(3.5)
    assert peaks.parent_cores == pytest.approx(2.0)
    assert peaks.children_cores == pytest.approx(1.5)
    assert peaks.samples == 4
    assert peaks.root == 1
    assert peaks.ticks == {1: 10 + 3 * t, 2: 10 + t, 3: t // 2}
    peaks.store(12)
    assert (peaks.storage_bytes, peaks.samples) == (12, 4)


def test_peaks_split_window_cores_between_the_root_and_its_children():
    t = sampling.TICKS
    peaks = sampling.Peaks()
    peaks.add(sampling.Snapshot(0.0, 2, 1, 1, 1, {7: t, 8: 0}, 7), 0)
    peaks.add(sampling.Snapshot(2.0, 2, 1, 1, 1, {7: 2 * t, 8: 6 * t}, 7), 0)
    assert peaks.cores == pytest.approx(3.5)
    assert peaks.parent_cores == pytest.approx(0.5)
    assert peaks.children_cores == pytest.approx(3.0)
    assert peaks.anchor == (2.0, 8 * t, 2 * t)


def test_peaks_ignore_a_snapshot_inside_the_window():
    peaks = sampling.Peaks()
    peaks.add(sampling.Snapshot(1.0, 1, 1, 1, 1, {1: 0}), 0)
    peaks.add(sampling.Snapshot(1.0 + sampling.CORE_WINDOW / 2, 1, 1, 1, 1, {1: 500}), 0)
    assert peaks.cores == 0.0


def test_summary_rounds_every_figure_and_takes_the_larger_cpu_count():
    mib = sampling.MIB
    peaks = sampling.Peaks(
        processes=3,
        children_processes=2,
        rss_bytes=5 * mib + 12345,
        pss_bytes=3 * mib + 6789,
        parent_rss_bytes=mib + 7000,
        children_rss_bytes=4 * mib + 9000,
        storage_bytes=2 * mib + 600000,
        cores=1.23456,
        parent_cores=0.43216,
        children_cores=0.98765,
        samples=7,
        root=1,
        ticks={1: sampling.TICKS, 2: sampling.TICKS},
    )
    usage = SimpleNamespace(ru_utime=2.25, ru_stime=0.50049)
    assert sampling.summary(peaks, usage, 3.14159, 256, False) == {
        "peak_rss_mib": 5.01,
        "peak_pss_mib": 3.01,
        "peak_parent_rss_mib": 1.01,
        "peak_children_rss_mib": 4.01,
        "peak_processes": 3,
        "peak_children_processes": 2,
        "peak_cores": 1.235,
        "peak_parent_cores": 0.432,
        "peak_children_cores": 0.988,
        "cpu_seconds": 2.75,
        "parent_cpu_seconds": 1.0,
        "children_cpu_seconds": 1.75,
        "wall_seconds": 3.142,
        "peak_storage_mib": 2.57,
        "samples": 7,
        "exit_code": 1,
        "timed_out": False,
    }
    lean = SimpleNamespace(ru_utime=0.25, ru_stime=0.5)
    floored = sampling.summary(peaks, lean, 0.8, 0, True)
    assert (floored["cpu_seconds"], floored["peak_cores"]) == (2.0, 2.5)
    assert (floored["parent_cpu_seconds"], floored["children_cpu_seconds"]) == (1.0, 1.0)
    assert (floored["peak_parent_cores"], floored["peak_children_cores"]) == (1.25, 1.25)


def test_summary_counts_no_parent_cpu_when_the_root_was_never_sampled(monkeypatch):
    monkeypatch.setattr(sampling, "TICKS", 7)
    usage = SimpleNamespace(ru_utime=0.0, ru_stime=0.0)
    unsampled = sampling.summary(sampling.Peaks(root=5, ticks={6: 7}), usage, 1.0, 0, False)
    assert (unsampled["parent_cpu_seconds"], unsampled["children_cpu_seconds"]) == (0.0, 1.0)
    sampled = sampling.summary(sampling.Peaks(root=5, ticks={5: 1, 6: 6}), usage, 1.0, 0, False)
    assert (sampled["parent_cpu_seconds"], sampled["children_cpu_seconds"]) == (0.143, 0.857)


def test_directory_bytes_counts_regular_files_and_not_link_targets(tmp_path):
    tree = tmp_path / "tree"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "one").write_bytes(b"x" * 1000)
    (tree / "two").write_bytes(b"y" * 24)
    (tree / "link").symlink_to(tree / "a" / "one")
    assert sampling.directory_bytes(tree) == 1024
    assert sampling.directory_bytes(tree / "missing") == 0


def test_size_of_a_vanished_entry_is_zero(tmp_path):
    assert sampling._size(tmp_path / "gone") == 0


def allocated_here(source) -> bool:
    if source is None:
        return True
    trace = tracemalloc.get_object_traceback(source)
    return trace is not None and any(frame.filename in (__file__, sampling.__file__) for frame in trace)


def test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd):
    command = [sys.executable, str(WORKLOADS / "memory.py"), "--children", "2", "--mib", "48", "--hold", "1.5"]
    gc.collect()
    limit = tracemalloc.get_traceback_limit() if tracemalloc.is_tracing() else 0
    tracemalloc.stop()
    tracemalloc.start(TRACE_FRAMES)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = sampling.measure(command, tmp_path, {"PATH": os.environ["PATH"]}, 0.05, 60)
            gc.collect()
        leaks = [
            f"{w.category.__name__}: {w.message} at {w.filename}:{w.lineno}"
            for w in caught
            if issubclass(w.category, ResourceWarning) and allocated_here(w.source)
        ]
    finally:
        tracemalloc.stop()
        if limit:
            tracemalloc.start(limit)
    assert leaks == [], " | ".join(leaks)
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert 3 <= result["peak_processes"] < 10
    assert 96 <= result["peak_rss_mib"] < 2048
    assert 96 <= result["peak_pss_mib"] < 2048
    assert 96 <= result["peak_children_rss_mib"] < result["peak_rss_mib"]
    assert 0 < result["peak_parent_rss_mib"] < 96
    assert 0 < result["cpu_seconds"] < 30
    assert result["parent_cpu_seconds"] + result["children_cpu_seconds"] == pytest.approx(
        result["cpu_seconds"], abs=0.002
    )
    assert 0 < result["children_cpu_seconds"]
    assert result["peak_children_processes"] == result["peak_processes"] - 1
    assert 0 < result["peak_cores"] <= os.cpu_count() * 2
    assert 0 < result["peak_children_cores"] <= result["peak_cores"]
    assert result["samples"] >= 5
    assert 1.0 <= result["wall_seconds"] < 30


def leaked_file(path):
    cycle = [path.open("w")]
    cycle.append(cycle)


FOREIGN_LEAK = compile("cycle = [open(path, 'w')]\ncycle.append(cycle)", "<another test's thread>", "exec")


def leaked_by_another_thread(path):
    thread = threading.Thread(target=exec, args=(FOREIGN_LEAK, {"path": str(path)}))
    thread.start()
    thread.join()


@pytest.mark.flaky(reruns=0)
@pytest.mark.parametrize("case", ["inherited", "foreign", "new-leak"])
def test_live_workload_resource_audit_owns_only_sampling_leaks(tmp_path, capfd, monkeypatch, case):
    enabled = gc.isenabled()
    gc.disable()
    leak = tmp_path / f"{case}-resource"
    try:
        if case == "inherited":
            leaked_file(leak)
            test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd)
        elif case == "foreign":
            measure = sampling.measure

            def measure_beside_another_thread(*args, **kwargs):
                result = measure(*args, **kwargs)
                leaked_by_another_thread(leak)
                return result

            monkeypatch.setattr(sampling, "measure", measure_beside_another_thread)
            test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd)
        else:
            measure = sampling.measure

            def leaking_measure(*args, **kwargs):
                result = measure(*args, **kwargs)
                leaked_by_another_thread(tmp_path / "foreign")
                leaked_file(leak)
                return result

            monkeypatch.setattr(sampling, "measure", leaking_measure)
            with pytest.raises(AssertionError) as failure:
                test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd)
            summary = str(failure.value).splitlines()[0]
            assert "ResourceWarning" in summary
            assert "unclosed file" in summary
            assert str(leak) in summary
            assert __file__ in summary
            assert str(tmp_path / "foreign") not in summary
    finally:
        gc.collect()
        if enabled:
            gc.enable()


def test_measure_counts_peak_storage_written_then_removed(tmp_path):
    script = (
        "import os, time, pathlib; p = pathlib.Path('blob'); p.write_bytes(bytes(3 << 20)); "
        "time.sleep(0.5); p.unlink(); time.sleep(0.3)"
    )
    result = sampling.measure([sys.executable, "-c", script], tmp_path, {}, 0.05, 30)
    assert result["peak_storage_mib"] == pytest.approx(3.0, abs=0.01)


def test_measure_counts_storage_left_at_exit(tmp_path):
    script = "import pathlib; pathlib.Path('blob').write_bytes(bytes(2 << 20))"
    result = sampling.measure([sys.executable, "-c", script], tmp_path, {}, 1.0, 30)
    assert result["peak_storage_mib"] == pytest.approx(2.0, abs=0.01)


def test_measure_passes_only_the_given_environment_and_silences_output(tmp_path, capfd, monkeypatch):
    monkeypatch.setenv("SV2_PROBE_LEAK", "leaked")
    script = "import os, sys; print('probe-output'); print('probe-error', file=sys.stderr); sys.exit(4 if 'SV2_PROBE_LEAK' in os.environ else 0)"
    result = sampling.measure([sys.executable, "-c", script], tmp_path, {"SV2_GIVEN": "1"}, 0.05, 30)
    captured = capfd.readouterr()
    assert result["exit_code"] == 0
    assert "probe-output" not in captured.out and "probe-error" not in captured.err


def test_measure_runs_the_command_in_the_workdir(tmp_path):
    script = "import os, pathlib; pathlib.Path('cwd').write_text(os.getcwd())"
    sampling.measure([sys.executable, "-c", script], tmp_path, {}, 0.05, 30)
    assert (tmp_path / "cwd").read_text() == str(tmp_path)


def test_measure_keeps_sampling_at_exactly_the_timeout(tmp_path, monkeypatch):
    readings = iter([0.0])
    clock = SimpleNamespace(monotonic=lambda: next(readings, 10.0), sleep=lambda seconds: time.sleep(1.0))
    monkeypatch.setattr(sampling, "time", clock)
    result = sampling.measure([sys.executable, "-c", "import time; time.sleep(0.3)"], tmp_path, {}, 0.05, 10.0)
    assert not result["timed_out"] and result["exit_code"] == 0
    assert result["wall_seconds"] == 10.0


def test_measure_stops_sampling_once_the_command_has_exited(tmp_path, monkeypatch):
    readings = iter(range(100))
    clock = SimpleNamespace(monotonic=lambda: float(next(readings)), sleep=lambda seconds: None)
    monkeypatch.setattr(sampling, "time", clock)
    monkeypatch.setattr(sampling, "_exited", lambda pid: True)
    result = sampling.measure(["true"], tmp_path, {"PATH": os.environ["PATH"]}, 0.05, 20.0)
    assert result["samples"] == 0 and not result["timed_out"]


def test_measure_kills_the_whole_group_on_timeout(tmp_path):
    marker = tmp_path / "pid"
    script = (
        f"import os, subprocess, time; p = subprocess.Popen(['sleep', '60']); "
        f"open({str(marker)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    env = {"PATH": os.environ["PATH"]}
    result = sampling.measure([sys.executable, "-c", script], tmp_path, env, 0.05, 1.0)
    assert result["timed_out"] and result["exit_code"] == -9
    assert 1.0 <= result["wall_seconds"] < 10
    assert killed(int(marker.read_text()))


def test_measure_kills_stragglers_left_by_a_finished_command(tmp_path):
    marker = tmp_path / "pid"
    script = f"import subprocess; p = subprocess.Popen(['sleep', '60']); open({str(marker)!r}, 'w').write(str(p.pid))"
    result = sampling.measure([sys.executable, "-c", script], tmp_path, {"PATH": os.environ["PATH"]}, 0.05, 30)
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert killed(int(marker.read_text()))
