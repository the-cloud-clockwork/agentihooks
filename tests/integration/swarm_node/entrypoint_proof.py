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
    outcomes = [
        run_case(args.image, mode, args.output, supervised=True, viewer=False) for mode in ("complete", "forced")
    ]
    for outcome in outcomes:
        result = outcome["result"]
        assert result["reason"] == "termination" and result["signal"] == 15
        assert result["supervisor_child_exit_total"] >= 4
    outcomes.append(run_case(args.image, "complete", args.output, viewer=False))
    (args.output / "entrypoint-result.json").write_text(json.dumps(outcomes, indent=2) + "\n")
    print("supervisor process one drained on TERM and removed cooperative and stubborn children; generic Tini passed")


if __name__ == "__main__":
    main()
