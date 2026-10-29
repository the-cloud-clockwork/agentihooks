import argparse
import json
import os
import sys
from pathlib import Path

from scripts.evidence import schema
from scripts.headless import runner
from scripts.headless.request import HeadlessError
from scripts.scout import loop


def rounds(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("rounds must be at least 1")
    return count


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentihooks scout", description="Answer a question with an evidence set")
    parser.add_argument("question")
    parser.add_argument("--until", help="condition the evidence set must meet")
    parser.add_argument("--rounds", type=rounds, default=loop.DEFAULT_ROUNDS)
    parser.add_argument("--tier", choices=("low", "high"), default="low")
    parser.add_argument("--harness", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--out", type=Path, help="file the evidence set is written to")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse(argv)
    try:
        harness = runner.harness_for(args.harness, os.environ)
        scout = loop.Scout(args.question, args.until, args.rounds, args.tier, harness)
        evidence = loop.run(scout)
        if not evidence["items"]:
            raise ValueError(f"no source supported an answer after {args.rounds} rounds")
        schema.validate("evidence", evidence)
    except (ValueError, HeadlessError, schema.SchemaError) as error:
        print(f"scout: {error}", file=sys.stderr)
        return 1
    text = json.dumps(evidence, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0
