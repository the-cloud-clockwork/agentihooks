import json
import os
import pstats
import subprocess
import sys
import time
from pathlib import Path

SOURCE = "41cb08e5926b15ae9b582ecd6036b7b501c98d2c"
BASES = "5028d9e6b0c0f1083f3f955779d1a7caf2f8e960,02a9f46bdae8bf88a0868d0e36cd4d89bc5db0fe"
BOOTSTRAP = """import atexit
import cProfile
import json
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

_directory = Path(os.environ.get("MUTATION_PROFILE_DIR", "."))
_real_exit = os._exit
_profile = cProfile.Profile(timer=time.process_time)
_wall = time.monotonic()
_cpu = time.process_time()
_done = False

def _dump() -> None:
    global _done
    if _done:
        return
    _done = True
    _profile.disable()
    pid = os.getpid()
    _profile.dump_stats(str(_directory / f"{pid}.prof"))
    (_directory / f"{pid}.json").write_text(json.dumps({
        "pid": pid,
        "ppid": os.getppid(),
        "entry": Path(sys.argv[0]).name,
        "wall_seconds": time.monotonic() - _wall,
        "cpu_seconds": time.process_time() - _cpu,
    }))

def _forked() -> None:
    global _profile, _wall, _cpu, _done
    _profile.disable()
    _profile = cProfile.Profile(timer=time.process_time)
    _wall = time.monotonic()
    _cpu = time.process_time()
    _done = False
    _profile.enable()

def _exit(status: int) -> NoReturn:
    _dump()
    _real_exit(status)

if os.environ.get("MUTATION_PROFILE_DIR"):
    os.register_at_fork(after_in_child=_forked)
    os._exit = _exit
    atexit.register(_dump)
    _profile.enable()
"""


def test_profile_one_recorded_stats_part(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    reports = Path(os.environ["PROOF_REPORT_DIR"]) / "stats-candidates-profile"
    reports.mkdir(parents=True, exist_ok=True)
    inputs = json.loads((root / "tests/stats_candidates_profile_input.json").read_text())
    nodes = inputs["nodes"]
    assert len(nodes) == len(set(nodes)) == 1046
    duration_blob = subprocess.check_output(["git", "hash-object", ".test_durations"], cwd=root, text=True).strip()
    assert duration_blob == "bb86000192dfae9e1606ea6ee21348fef4c9f34f"
    selection = {path: nodes for path in inputs["scope"]}
    (root / ".mutation-selection.json").write_text(json.dumps(selection))
    split_program = """import json
import os
from pathlib import Path
from scripts.ci_mutation.selection import split_files, split_key, stats_shards, worker_count
nodes = json.loads(Path('tests/stats_candidates_profile_input.json').read_text())['nodes']
split = split_files(Path.cwd(), nodes, 4)
workers = worker_count(len(os.sched_getaffinity(0)))
buckets = stats_shards(Path.cwd(), split[0], workers)
print(json.dumps({'split': split, 'key': split_key(split), 'workers': workers, 'buckets': buckets}))
"""
    split_data = json.loads(subprocess.check_output([sys.executable, "-c", split_program], cwd=root, text=True))
    (reports / "split.json").write_text(json.dumps(split_data))
    assert [len(part) for part in split_data["split"]] == [262, 259, 263, 262]
    assert split_data["key"].startswith("62ef409f0334")
    loader = reports / "loader"
    loader.mkdir()
    (loader / "sitecustomize.py").write_text(BOOTSTRAP)
    profiles = reports / "profiles"
    profiles.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "MUTATION_PROFILE_DIR": str(profiles),
            "MUTATION_STATS_CACHE": str(tmp_path / "empty-stats-cache"),
            "PYTHONPATH": str(loader),
            "PYTHONUNBUFFERED": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "scripts.ci_mutation",
        "--bases",
        BASES,
        "--head",
        SOURCE,
        "--budget",
        "1080",
        "--output",
        str(reports / "gate"),
        "--stats",
        str(reports / "stats"),
        "--stats-part",
        "0",
        "--stats-parts",
        "4",
    ]
    start = time.monotonic()
    with (reports / "run.log").open("w") as output:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=720,
            check=False,
        )
    metadata = [json.loads(path.read_text()) for path in sorted(profiles.glob("*.json"))]
    summary = {
        "source": SOURCE,
        "bases": BASES,
        "duration_blob": duration_blob,
        "nodes": len(nodes),
        "split": split_data["key"],
        "part_nodes": len(split_data["split"][0]),
        "workers": split_data["workers"],
        "bucket_count": len(split_data["buckets"]),
        "wall_seconds": time.monotonic() - start,
        "status": result.returncode,
        "processes": metadata,
    }
    (reports / "summary.json").write_text(json.dumps(summary, indent=2))
    for path in sorted(path for path in profiles.glob("*.prof") if path.stat().st_size):
        with path.with_suffix(".txt").open("w") as output:
            stats = pstats.Stats(str(path), stream=output).strip_dirs()
            stats.sort_stats("cumulative").print_stats(60)
            stats.sort_stats("tottime").print_stats(60)
    print(json.dumps(summary, indent=2), flush=True)
    assert result.returncode == 0, (reports / "run.log").read_text()[-12000:]
    assert len(metadata) >= 3 + len(split_data["buckets"])
    part = json.loads((reports / "stats/part-0.json").read_text())
    assert part["split"] == split_data["key"]
    assert len(part["results"]) == len(split_data["buckets"])
    selected = set(split_data["split"][0])
    measured = {
        function: sorted({entry for bucket in part["results"] for entry in bucket["tests"].get(function, [])})
        for function in inputs["mapping"]
    }
    expected = {function: sorted(set(entries) & selected) for function, entries in inputs["mapping"].items()}
    (reports / "mapping.json").write_text(json.dumps({"expected": expected, "measured": measured}, indent=2))
    assert measured == expected

    traced_tests = sum(len(bucket["durations"]) for bucket in part["results"])
    traced_seconds = sum(sum(bucket["durations"].values()) for bucket in part["results"])
    comparison = {
        "before_tests": inputs["baseline_traced_tests"],
        "after_tests": traced_tests,
        "before_traced_seconds": inputs["baseline_traced_seconds"],
        "after_traced_seconds": traced_seconds,
        "mapping_equal": measured == expected,
    }
    (reports / "comparison.json").write_text(json.dumps(comparison, indent=2))
    print(json.dumps(comparison), flush=True)
    assert traced_tests < inputs["baseline_traced_tests"]
    assert traced_seconds < inputs["baseline_traced_seconds"]
