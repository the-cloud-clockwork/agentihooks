import gzip
import importlib.util
import json
import subprocess
import sys
import warnings
from pathlib import Path
from time import perf_counter

import pytest

from scripts.ci_mutation import stats_cache
from scripts.ci_mutation.stats_dependencies import StoredStats


@pytest.mark.timeout(840)
def test_archived_statistics_processing(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(gzip.decompress((root / "tests/fixtures/mutation_statistics_processing.json.gz").read_bytes()))
    fresh = payload["fresh"]
    for result in fresh:
        result["dependencies"] = {
            node: [path for path in payload["dependency_sets"][index] if (root / path).is_file()]
            for node, index in result["dependencies"].items()
        }
    files = [path for result in fresh for path in result["files"] if (root / path).is_file()]
    base_file = tmp_path / "base.py"
    base_file.write_bytes(
        subprocess.check_output(["git", "show", "74f666fb75:scripts/ci_mutation/stats_cache.py"], cwd=root)
    )
    spec = importlib.util.spec_from_file_location("statistics_cache_base", base_file)
    base = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)
    outcomes = []
    for side, module in [("base", base), ("head", stats_cache)]:
        timings = {}

        def timed(name, function):
            def call(*args, **kwargs):
                start = perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    timings[name] = timings.get(name, 0) + perf_counter() - start

            return call

        with monkeypatch.context() as patch:
            patch.setattr(module, "per_test_stats", timed("slicing", module.per_test_stats))
            patch.setattr(module.StatsInputs, "for_test", timed("inputs", module.StatsInputs.for_test))
            patch.setattr(StoredStats, "save", timed("persistence", StoredStats.save))
            folder = tmp_path / side
            start = perf_counter()
            module.collect_cached_stats(root, folder, {}, files, [], lambda chosen: fresh)
            timings["total"] = perf_counter() - start
            warnings.warn(side + " Statistics processing seconds: " + json.dumps(timings, sort_keys=True), stacklevel=1)
        outcomes.append({path.name: json.loads(path.read_text()) for path in folder.glob("*.json")})
    assert outcomes[0] == outcomes[1]
    print("Identical cached records:", len(outcomes[0]), flush=True)
