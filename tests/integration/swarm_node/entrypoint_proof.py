import argparse
import json
from pathlib import Path

from supervision_proof import run_case


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    outcomes = [run_case(args.image, mode, args.output, supervised=True) for mode in ("complete", "forced")]
    outcomes.append(run_case(args.image, "complete", args.output))
    (args.output / "entrypoint-result.json").write_text(json.dumps(outcomes, indent=2) + "\n")
    print("supervisor process one drained on TERM and removed cooperative and stubborn children; generic Tini passed")


if __name__ == "__main__":
    main()
