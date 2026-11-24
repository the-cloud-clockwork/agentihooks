"""Proof: how many tracked test modules the stats cache's static closure lets reach a source file.

Run outside the checkout with `python -I lab_closure_reach.py --repo CHECKOUT`. It builds the closure of every
tracked test module as `expand_mutation.stats_units` does and prints, for each probe file, how many test modules
hold it and the recorded seconds of their tests.
"""

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

PROBES = (
    "scripts/term_colors.py",
    "scripts/claude_login.py",
    "scripts/ci_lab/executors.py",
    "scripts/ci_lab/scheduler.py",
    "hooks/config.py",
    "hooks/hook_manager.py",
    "scripts/install.py",
)
SHOWN = 25


def main(argv: Sequence[str]) -> int:
    """Print the closure sizes of the tracked test modules and the reach of each probe file."""
    parser = argparse.ArgumentParser(prog="lab_closure_reach")
    parser.add_argument("--repo", required=True)
    repo = Path(parser.parse_args(argv).repo).resolve()
    sys.path.insert(0, str(repo))
    from scripts.ci_mutation.scope import module_name
    from scripts.ci_mutation.stats_cache import TRACKED, UNKEYED, StatsInputs

    tracked = [path for path in subprocess.check_output(TRACKED, cwd=repo, text=True).split("\0") if path]
    present = [path for path in tracked if (repo / path).is_file()]
    modules = {module_name(Path(path)): path for path in tracked if Path(path).suffix == ".py"}
    resources: dict[str, set[str]] = {}
    for path in present:
        if path in UNKEYED:
            continue
        parts = Path(path).parts
        for index in range(len(parts)):
            resources.setdefault("/".join(parts[index:]), set()).add(path)
        for parent in Path(path).parents:
            resources.setdefault(str(parent) + "/", set()).add(path)
    inputs = StatsInputs(repo, modules, resources, {})
    tests = [
        path
        for path in present
        if path.startswith("tests/") and Path(path).name.startswith("test_") and path.endswith(".py")
    ]
    recorded: dict[str, float] = {}
    for name, seconds in json.loads((repo / ".test_durations").read_text()).items():
        owner = name.partition("::")[0]
        recorded[owner] = recorded.get(owner, 0.0) + seconds
    began = time.monotonic()
    closures = {path: inputs.for_test(path, None) for path in tests}
    print(f"{len(tests)} test modules, {len(modules)} python modules, closures took {time.monotonic() - began:.1f} s")
    sizes = sorted(len(found) for found in closures.values())
    sources = [path for path in modules.values() if path.startswith(("hooks/", "scripts/"))]
    whole = sum(1 for found in closures.values() if len(found.keys() & set(sources)) >= 0.9 * len(sources))
    print(f"closure sizes: least {sizes[0]}, median {sizes[len(sizes) // 2]}, most {sizes[-1]}")
    print(f"{whole} test modules hold nine tenths of the {len(sources)} source modules")
    print(f"recorded seconds of every test module: {sum(recorded.values()):.0f}")
    for probe in PROBES:
        holders = sorted(path for path, found in closures.items() if probe in found)
        seconds = sum(recorded.get(path, 0.0) for path in holders)
        print(f"{probe}: {len(holders)} test modules hold it, {seconds:.0f} recorded seconds")
        for path in holders[:SHOWN]:
            print(f"  {path} ({len(closures[path])} inputs, {recorded.get(path, 0.0):.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
