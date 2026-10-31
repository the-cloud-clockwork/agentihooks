"""Hold the measured mutation shard planner's batch runner ceiling equal to maxRunners of the antoncore batch chart.

The planner reads its ceiling from this head by parsing, never importing, so the protected grader's copy can grade it.
"""

import argparse
import ast
from pathlib import Path

import yaml

PLANNER = "scripts/ci_mutation/plan.py"
PLANNER_NAME = "BATCH_RUNNERS"
BATCH_VALUES = "k8s/charts/arc-runners-oss/values-oss-batch.yaml"


def planner_ceiling(root: Path) -> int | None:
    """Return the integer literal the planner assigns to its batch runner ceiling, or None without one."""
    for node in ast.parse((root / PLANNER).read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets] == [PLANNER_NAME]
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
        ):
            return node.value.value
    return None


def chart_ceiling(root: Path) -> int:
    """Return maxRunners of the antoncore batch runner scale set."""
    return yaml.safe_load((root / BATCH_VALUES).read_text())["gha-runner-scale-set"]["maxRunners"]


def problems(agentihooks: Path, antoncore: Path) -> list[str]:
    """Return one line per way the planner ceiling differs from the chart."""
    ours = planner_ceiling(agentihooks)
    if ours is None:
        return [f"{PLANNER} defines no integer literal {PLANNER_NAME}."]
    theirs = chart_ceiling(antoncore)
    if ours == theirs:
        return []
    return [
        f"{PLANNER} {PLANNER_NAME} is {ours} but antoncore {BATCH_VALUES} maxRunners is {theirs}; change them together."
    ]


def main(argv: list[str] | None = None) -> int:
    """Print the comparison and return 1 when the two ceilings differ."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentihooks", type=Path, required=True)
    parser.add_argument("--antoncore", type=Path, required=True)
    args = parser.parse_args(argv)
    found = problems(args.agentihooks, args.antoncore)
    for line in found:
        print(f"::error::{line}")
    if not found:
        print(
            f"{PLANNER} {PLANNER_NAME} and antoncore {BATCH_VALUES} maxRunners are both {chart_ceiling(args.antoncore)}."
        )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
