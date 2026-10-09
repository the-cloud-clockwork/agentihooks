import argparse
import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from scripts import ci_wiring

GATE = ci_wiring.GATE
WORKFLOWS = ".github/workflows"


def _gates(workflows: dict[str, dict]) -> list[tuple[str, str, dict]]:
    return [
        (file, job_id, job)
        for file, workflow in workflows.items()
        for job_id, job in (workflow.get("jobs") or {}).items()
        if job.get("name") == GATE
    ]


def _protected_config(base: dict, head: dict) -> dict:
    return {
        section: {key: entry for key, entry in base.get(section, {}).items() if key in head.get(section, {})}
        for section in ("not_gates", "outside_gate")
    }


def _aggregator_problems(base: tuple[str, str, dict], head: tuple[str, str, dict]) -> list[str]:
    (base_file, base_id, base_job), (head_file, head_id, head_job) = base, head
    if (base_file, base_id) != (head_file, head_id):
        return [f"The head moves {GATE} from {base_file}/{base_id} to {head_file}/{head_id}."]
    problems = [
        f"The head's {GATE} drops the base need {need}."
        for need in sorted(ci_wiring.needs_of(base_job) - ci_wiring.needs_of(head_job))
    ]
    fields = sorted((set(base_job) | set(head_job)) - {"needs"})
    return problems + [
        f"The head's {GATE} changes its {field}; the protected branch grades with its own."
        for field in fields
        if base_job.get(field) != head_job.get(field)
    ]


def grade(
    base_workflows: dict[str, dict], base_config: dict, head_workflows: dict[str, dict], head_config: dict, today: date
) -> list[str]:
    base_gates, head_gates = _gates(base_workflows), _gates(head_workflows)
    if len(base_gates) != 1:
        return [f"The base holds {len(base_gates)} jobs named {GATE}; nothing protected can grade."]
    if len(head_gates) != 1:
        return [f"The head holds {len(head_gates)} jobs named {GATE}; exactly one must be."]
    problems = _aggregator_problems(base_gates[0], head_gates[0])
    return problems + ci_wiring.check(head_workflows, _protected_config(base_config, head_config), today)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def load(root: Path, rev: str) -> tuple[dict[str, dict], dict]:
    tracked = _git(root, "ls-tree", "-r", "-z", "--name-only", rev).split("\0")
    workflows = {
        Path(path).name: yaml.safe_load(_git(root, "show", f"{rev}:{path}")) or {}
        for path in tracked
        if Path(path).parent == Path(WORKFLOWS) and path.endswith((".yml", ".yaml"))
    }
    config = json.loads(_git(root, "show", f"{rev}:{ci_wiring.CONFIG}")) if ci_wiring.CONFIG in tracked else {}
    return workflows, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default="HEAD")
    args = parser.parse_args(argv)
    root = Path(args.root)
    merge_base = _git(root, "merge-base", args.base, args.head).strip()
    base_workflows, branched_config = load(root, merge_base)
    _, tip_config = load(root, args.base)
    base_config = {
        section: {**branched_config.get(section, {}), **tip_config.get(section, {})}
        for section in ("not_gates", "outside_gate")
    }
    head_workflows, head_config = load(root, args.head)
    problems = grade(base_workflows, base_config, head_workflows, head_config, datetime.now(UTC).date())
    print(f"graded {args.head[:12]} against merge base {merge_base[:12]}: {len(problems)} protected gate problems")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
