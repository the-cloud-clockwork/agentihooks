from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hooks.classifier import decision_log
from hooks.classifier.core import decide
from hooks.classifier.errors import ClassifierInputError, ClassifierRequestError, ClassifierUnavailable
from hooks.classifier.questions import questions_from_wire


def _read_state(path: str) -> object:
    text = Path(path).read_text()
    try:
        return json.loads(text)
    except ValueError:
        return text


def classify_main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks classify", description="Ask the decision models typed questions")
    parser.add_argument("--state", required=True, help="File holding the state: JSON, or plain text")
    parser.add_argument("--questions", required=True, help="JSON file of named questions: type, instructions, criteria")
    parser.add_argument("--purpose", default="cli", help="Caller name recorded in the decision log")
    args = parser.parse_args(argv)
    try:
        questions = questions_from_wire(json.loads(Path(args.questions).read_text()))
        result = decide(_read_state(args.state), questions, purpose=args.purpose)
    except (ClassifierInputError, ClassifierRequestError) as error:
        print(f"classify: {error}", file=sys.stderr)
        return 2
    except ClassifierUnavailable as error:
        print(f"classify: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def classifier_main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks classifier", description="Decision classifier records")
    commands = parser.add_subparsers(dest="command", required=True)
    stats_parser = commands.add_parser("stats", help="Counts, sources, fallback rate and latency from the decision log")
    stats_parser.add_argument("--purpose", help="Only calls made for this purpose")
    args = parser.parse_args(argv)
    print(json.dumps(decision_log.stats(args.purpose), indent=2))
    return 0
