"""Grade head load gate runs against base runs interleaved on the same runner, each side by its best run."""

import argparse
import json
from pathlib import Path

MARGIN = 1.1
MEASURE = '{"seconds"'
BEST = (("write_p95_s", "write p95"), ("busiest_cores", "busiest CPU window"))


def graded_measure(log: str) -> dict | None:
    """The last measure line in the log: the one the gate graded."""
    lines = [line for line in log.splitlines() if line.startswith(MEASURE)]
    return json.loads(lines[-1]) if lines else None


def best(runs: list[dict | None]) -> dict | None:
    if not runs or None in runs:
        return None
    measured = {key: [run[key] for run in runs if run[key] is not None] for key, _ in BEST}
    return {"errors": max(run["errors"] for run in runs)} | {
        key: min(values) if values else None for key, values in measured.items()
    }


def regressions(base: dict | None, head: dict | None) -> list[str]:
    if base is None or head is None:
        return [f"the {'base' if base is None else 'head'} runs printed no load measure"]
    problems = []
    if head["errors"] > base["errors"]:
        problems.append(f"head had {head['errors']} failed requests, base {base['errors']}")
    for key, label in BEST:
        if base[key] is None or head[key] is None:
            problems.append(f"{label} was not measured on the {'base' if base[key] is None else 'head'}")
        elif head[key] > base[key] * MARGIN:
            problems.append(f"head {label} {head[key]} exceeds base {base[key]} by more than {MARGIN}x")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, nargs="+", required=True)
    parser.add_argument("--head", type=Path, nargs="+", required=True)
    args = parser.parse_args(argv)
    runs = {side: [graded_measure(path.read_text()) for path in paths] for side, paths in vars(args).items()}
    base, head = best(runs["base"]), best(runs["head"])
    print(json.dumps({"runs": runs, "base": base, "head": head}, indent=2), flush=True)
    problems = regressions(base, head)
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
