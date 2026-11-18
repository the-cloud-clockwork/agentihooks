import hashlib
import json
import os
import resource
import subprocess
import time
import warnings
from pathlib import Path

from scripts import type_gate

ROOT = Path(__file__).resolve().parents[1]
STREAM = "tests/swarm_ledger/test_stream.py"
MANAGER = "hooks/hook_manager.py"
ADDED = "hooks/_aaa_context_probe.py"


def _runner() -> dict[str, object]:
    facts: dict[str, object] = {"cpu_count": os.cpu_count(), "affinity": len(os.sched_getaffinity(0))}
    for name in ("cpu.max", "memory.max"):
        path = Path("/sys/fs/cgroup") / name
        facts[name] = path.read_text().strip() if path.is_file() else None
    return facts


def _measure(files: list[str] | None) -> tuple[dict[str, int], dict[str, object]]:
    checked = type_gate.sources(ROOT) if files is None else sorted(files)
    runs = -(-len(checked) // -(-len(checked) // type_gate._RUN))
    start = time.monotonic()
    counts = type_gate.measure(ROOT, files)
    facts = {
        "checked": len(checked),
        "run_size": runs,
        "seconds": round(time.monotonic() - start, 1),
        "peak_mb_so_far": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024,
        "errors": sum(counts.values()),
        "digest": hashlib.sha256(json.dumps(counts, sort_keys=True).encode()).hexdigest()[:16],
        "stream": counts.get(STREAM, 0),
        "manager": counts.get(MANAGER, 0),
    }
    return counts, facts


def _differences(left: dict[str, int], right: dict[str, int], paths: list[str]) -> dict[str, object]:
    moved = {path: (left.get(path, 0), right.get(path, 0)) for path in paths if left.get(path, 0) != right.get(path, 0)}
    return {
        "files_compared": len(paths),
        "files_differing": len(moved),
        "higher": sum(1 for a, b in moved.values() if b > a),
        "lower": sum(1 for a, b in moved.values() if b < a),
        "examples": dict(sorted(moved.items())[:12]),
    }


def _publish(label: str, report: dict[str, object]) -> None:
    text = f"{label} {json.dumps(report, sort_keys=True)}"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as handle:
            handle.write(f"```\n{text}\n```\n")
    warnings.warn(text, stacklevel=1)


def _head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def test_probe_counts_one_file_by_the_files_checked_with_it() -> None:
    sources = type_gate.sources(ROOT)
    pull_request = sorted({MANAGER} | type_gate.importers(ROOT, sources, {type_gate.module_name(MANAGER)}))
    baseline = type_gate.load(ROOT) or {}
    report: dict[str, object] = {
        "head": _head(),
        "runner": _runner(),
        "sources": len(sources),
        "baseline": {"stream": baseline.get(STREAM, 0), "manager": baseline.get(MANAGER, 0)},
    }
    _, report["stream_alone"] = _measure([STREAM])
    _, report["manager_alone"] = _measure([MANAGER])
    graded, report["pull_request_layout"] = _measure(pull_request)
    every, report["every_source_layout"] = _measure(None)
    report["pull_request_versus_every_source"] = _differences(every, graded, pull_request)
    _publish("CONTEXT PROBE", report)


def test_probe_counts_the_tree_again_after_one_empty_source_shifts_the_runs() -> None:
    report: dict[str, object] = {"head": _head(), "runner": _runner()}
    before, report["every_source_layout"] = _measure(None)
    (ROOT / ADDED).write_text("")
    try:
        after, report["every_source_layout_one_added"] = _measure(None)
    finally:
        (ROOT / ADDED).unlink()
    report["shifted_versus_every_source"] = _differences(before, after, sorted(set(before) | set(after)))
    _publish("SHIFT PROBE", report)
