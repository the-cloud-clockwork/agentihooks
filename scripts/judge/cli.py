import argparse
import json
import os
import sys
from pathlib import Path

from hooks.filters.check import check
from scripts.evidence import schema
from scripts.headless import runner, tiers
from scripts.headless.request import SEALED, HeadlessError
from scripts.judge import record

ROLE = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles" / "judge" / "CLAUDE.md"
EFFORT = "medium"
INVALID = 2
SENT_BACK = 3


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentihooks judge", description="Judge a case against an evidence set")
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tier", choices=("high", "low"), default="high")
    parser.add_argument("--harness", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--out", type=Path, help="verdict record path, default <case>.verdict.json")
    return parser.parse_args(argv)


def judge_input(case: dict, evidence: dict) -> str:
    items = schema.shuffled(evidence["items"])
    return json.dumps({"case": case, "evidence": {"items": items}}, indent=2)


def main(argv: list[str]) -> int:
    args = parse(argv)
    try:
        case = schema.load_case(args.case)
        evidence = schema.load_evidence(args.evidence)
    except (OSError, schema.SchemaError) as error:
        print(f"judge: {error}", file=sys.stderr)
        return INVALID
    outcome = check("judge", {"case": case, "evidence": evidence})
    if outcome.decision == "deny":
        print(json.dumps({"sent_back": True, "findings": outcome.reason.splitlines()}, indent=2))
        return SENT_BACK
    screened = outcome.tool_input if outcome.decision == "rewrite" else {"case": case, "evidence": evidence}
    harness = runner.harness_for(args.harness, os.environ)
    verdict_schema = json.loads((schema.SCHEMAS / "verdict.schema.json").read_text())
    try:
        verdict = runner.run(
            ROLE.read_text(),
            judge_input(screened["case"], screened["evidence"]),
            verdict_schema,
            SEALED,
            args.tier,
            EFFORT,
            harness,
        )
        schema.validate("verdict", verdict)
    except (HeadlessError, schema.SchemaError) as error:
        print(f"judge: {error}", file=sys.stderr)
        return 1
    entry = record.build(case, evidence, verdict, harness, tiers.model(harness, args.tier, os.environ))
    record.write(args.out or args.case.with_suffix(".verdict.json"), entry)
    print(json.dumps(verdict, indent=2))
    return 0
