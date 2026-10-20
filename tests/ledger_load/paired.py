"""Grade head load gate runs against base runs paired on one runner, by the median head over base ratio of the pairs."""

import argparse
import json
from pathlib import Path
from statistics import median

MARGIN = 1.1
MEASURE = '{"seconds"'
GRADED = (("write_p50_s", "write p50"), ("busiest_cores", "busiest CPU window"))
SIDES = ("base", "head")


def graded_measure(log: str) -> dict | None:
    """The last measure line in the log: the one the gate graded."""
    lines = [line for line in log.splitlines() if line.startswith(MEASURE)]
    return json.loads(lines[-1]) if lines else None


def pair_ratios(pairs: list[tuple[dict, dict]], key: str) -> list[float]:
    return [head[key] / base[key] for base, head in pairs if base[key] and head[key] is not None]


def same_code_bound(runs: list[list[tuple[dict, dict]]]) -> float:
    """The widest single pair ratio, in either direction, of any recorded same code run."""
    ratios = [ratio for pairs in runs for key, _ in GRADED for ratio in pair_ratios(pairs, key)]
    return max((max(ratio, 1 / ratio) for ratio in ratios if ratio), default=1.0)


def recorded_runs(path: Path) -> list[list[tuple[dict, dict]]]:
    return list(json.loads(path.read_text())["runs"].values())


def regressions(pairs: list[tuple[dict | None, dict | None]], margin: float) -> list[str]:
    problems = [
        f"the {side} run of pair {number} printed no load measure"
        for number, pair in enumerate(pairs, 1)
        for side, run in zip(SIDES, pair)
        if run is None
    ]
    if problems or not pairs:
        return problems or ["no paired runs were given"]
    base_errors, head_errors = (sum(pair[index]["errors"] for pair in pairs) for index in (0, 1))
    if head_errors > base_errors:
        problems.append(f"head had {head_errors} failed requests, base {base_errors}")
    for key, label in GRADED:
        unmeasured = [
            f"{label} was not measured on the {side} run of pair {number}"
            for number, pair in enumerate(pairs, 1)
            for side, run in zip(SIDES, pair)
            if run[key] is None or side == "base" and not run[key]
        ]
        if unmeasured:
            problems += unmeasured
        elif (ratio := median(pair_ratios(pairs, key))) > margin:
            problems.append(f"head {label} median pair ratio {ratio:.2f} exceeds the {margin}x margin")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, nargs="+", required=True)
    parser.add_argument("--head", type=Path, nargs="+", required=True)
    parser.add_argument("--margin", type=float, default=MARGIN)
    parser.add_argument("--same-code", type=Path)
    args = parser.parse_args(argv)
    if len(args.base) != len(args.head):
        parser.error("--base and --head must name the same number of runs, one per pair")
    pairs = [
        tuple(graded_measure(path.read_text()) if path.exists() else None for path in pair)
        for pair in zip(args.base, args.head)
    ]
    measured = [pair for pair in pairs if None not in pair]
    bound = same_code_bound(recorded_runs(args.same_code)) if args.same_code else None
    ratios = {key: pair_ratios(measured, key) for key, _ in GRADED}
    print(json.dumps({"pairs": pairs, "ratios": ratios, "margin": args.margin, "same_code_bound": bound}, indent=2))
    problems = regressions(pairs, args.margin)
    if bound is not None and args.margin < bound:
        problems.insert(
            0, f"the margin {args.margin}x is below {bound:.2f}x, the widest pair ratio of the same code runs"
        )
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
