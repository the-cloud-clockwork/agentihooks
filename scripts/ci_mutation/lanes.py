import argparse
import json
import os
from pathlib import Path

import yaml


def allocation(ceiling: int, shards: int, run_id: int) -> tuple[int, int]:
    if ceiling < 1 or shards < 1:
        raise ValueError("Batch ceiling and stage width must be positive")
    lanes = max(1, ceiling // shards)
    return lanes, run_id % lanes


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--values", type=Path)
    source.add_argument("--ceiling", type=int)
    parser.add_argument("--shards", type=json.loads, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    ceiling = (
        yaml.safe_load(args.values.read_text())["gha-runner-scale-set"]["maxRunners"] if args.values else args.ceiling
    )
    lanes, lane = allocation(ceiling, len(args.shards), args.run_id)
    result = f"ceiling={ceiling}\nlanes={lanes}\nlane={lane}\n"
    print(result, end="")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
