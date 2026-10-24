"""Grade head hierarchy CPU per read against base runs paired on one runner by the geometric mean pair ratio."""

import argparse
import json
from math import exp, log
from pathlib import Path

MARGIN = 1.3
SIDES = ("base", "head")


def read_report(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def geometric_mean(ratios: list[float]) -> float:
    return exp(sum(map(log, ratios)) / len(ratios))


def same_code_bound(runs: list[list[list[float]]]) -> float:
    """The widest geometric mean pair ratio, in either direction, of any recorded same code run."""
    means = [geometric_mean([head / base for base, head in pairs]) for pairs in runs if pairs]
    return max((max(mean, 1 / mean) for mean in means), default=1.0)


def recorded_runs(path: Path) -> list[list[list[float]]]:
    return list(json.loads(path.read_text())["runs"].values())


def regressions(pairs: list[tuple[dict | None, dict | None]], margin: float) -> list[str]:
    problems = [
        f"the {side} run of pair {number} wrote no hierarchy report"
        for number, pair in enumerate(pairs, 1)
        for side, run in zip(SIDES, pair)
        if run is None
    ]
    if problems or not pairs:
        return problems or ["no paired runs were given"]
    for number, (base, head) in enumerate(pairs, 1):
        problems += [
            f"hierarchy CPU was not measured on the {side} run of pair {number}"
            for side, run in zip(SIDES, (base, head))
            if not run["mean_cpu_seconds"]
        ]
        outputs = [[row["outputs"] for row in run["observations"]] for run in (base, head)]
        if base["source"] != head["source"] or base["requests"] != head["requests"] or outputs[0] != outputs[1]:
            problems.append(f"hierarchy responses of pair {number} differ between base and head")
    if not problems:
        ratio = geometric_mean([head["mean_cpu_seconds"] / base["mean_cpu_seconds"] for base, head in pairs])
        if ratio > margin:
            problems.append(f"head hierarchy CPU geometric mean pair ratio {ratio:.2f} exceeds the {margin}x margin")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, nargs="+", required=True)
    parser.add_argument("--margin", type=float, default=MARGIN)
    parser.add_argument("--same-code", type=Path)
    args = parser.parse_args(argv)
    pairs = [tuple(read_report(folder / f"{side}.json") for side in SIDES) for folder in args.pairs]
    bound = same_code_bound(recorded_runs(args.same_code)) if args.same_code else None
    cpu = [[run and run["mean_cpu_seconds"] for run in pair] for pair in pairs]
    print(json.dumps({"base_head_cpu_seconds_per_read": cpu, "margin": args.margin, "same_code_bound": bound}))
    problems = regressions(pairs, args.margin)
    if bound is not None and args.margin < bound:
        problems.insert(
            0, f"the margin {args.margin}x is below {bound:.2f}x, the widest geometric mean of the same code runs"
        )
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
