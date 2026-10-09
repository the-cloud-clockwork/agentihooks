import argparse
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

GATE = "Gate — Required"
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target", "merge_group")
GATE_EVENTS = ("pull_request", "merge_group")
FILTERED_EVENTS = ("pull_request", "push")
FILTERS = ("paths", "paths-ignore", "types")
CONFIG = ".github/gate-wiring.json"
FIELDS = ("reason", "owner", "expires")
EVENT_TERM = re.compile(r"""github\.event_name\s*(==|!=)\s*(['"])(\w+)\2""")
EXPRESSION = re.compile(r"\$\{\{(.*)\}\}", re.DOTALL)


def triggers(workflow: dict) -> dict[str, dict]:
    on = workflow.get("on", workflow.get(True)) or {}
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        return {event: {} for event in on}
    return {event: config or {} for event, config in on.items()}


def needs_of(job: dict) -> set[str]:
    needs = job.get("needs") or []
    return {needs} if isinstance(needs, str) else set(needs)


def _skips_pull_requests(term: str) -> bool:
    operands = re.split(r"(==|!=)", term)
    if len(operands) == 3 and operands[2].strip() == "github.event_name":
        term = f"github.event_name {operands[1]} {operands[0]}"
    match = EVENT_TERM.fullmatch(term.strip())
    if match is None:
        return False
    operator, _, event = match.groups()
    return event not in PULL_REQUEST_EVENTS if operator == "==" else event == "pull_request"


def on_pull_requests(job: dict) -> bool:
    condition = str(job.get("if")).strip()
    wrapped = EXPRESSION.fullmatch(condition)
    condition = wrapped.group(1) if wrapped else condition
    return "||" in condition or not any(_skips_pull_requests(term) for term in condition.split("&&"))


def load(root: Path) -> dict[str, dict]:
    folder = root / ".github/workflows"
    paths = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    return {path.name: yaml.safe_load(path.read_text()) or {} for path in paths}


def _gate_problems(file: str, workflow: dict) -> list[str]:
    on = triggers(workflow)
    problems = [f"{file} holds {GATE} but does not run on {event}." for event in GATE_EVENTS if event not in on]
    for event in FILTERED_EVENTS:
        for key in FILTERS:
            if key in on.get(event, {}):
                problems.append(f"{file} filters its {event} trigger by {key}, so a core gate skips some changes.")
    return problems


def _job_problems(file: str, gate_id: str, jobs: dict, not_gates: dict) -> list[str]:
    needs = needs_of(jobs[gate_id])
    outside = [
        f"{file}/{job_id}"
        for job_id, job in jobs.items()
        if job_id != gate_id and job_id not in needs and on_pull_requests(job)
    ]
    problems = [f"{key} runs on pull requests but is not a need of {GATE}." for key in outside if key not in not_gates]
    return problems + [
        f"{key} is declared a non gate but names no pull request job of {file} outside the needs of {GATE}."
        for key in not_gates
        if key not in outside
    ]


def _outside_problems(workflows: dict[str, dict], gate_file: str, outside: dict) -> list[str]:
    jobs = [
        f"{file}/{job_id}"
        for file, workflow in workflows.items()
        if file != gate_file and any(event in triggers(workflow) for event in PULL_REQUEST_EVENTS)
        for job_id, job in (workflow.get("jobs") or {}).items()
        if on_pull_requests(job)
    ]
    problems = [
        f"{key} runs on pull requests outside {gate_file}, so it cannot be a need of {GATE}."
        for key in jobs
        if key not in outside
    ]
    return problems + [
        f"{key} is declared outside the gate but names no pull request job outside {gate_file}."
        for key in outside
        if key not in jobs
    ]


def _declaration_problems(declared: dict[str, dict], today: date) -> list[str]:
    problems = []
    for key, entry in declared.items():
        problems += [f"{key} is declared without its {field}." for field in FIELDS if not entry.get(field)]
        if entry.get("expires") and date.fromisoformat(entry["expires"]) < today:
            problems.append(f"{key} expired on {entry['expires']}.")
    return problems


def check(workflows: dict[str, dict], config: dict, today: date) -> list[str]:
    not_gates, outside = config.get("not_gates", {}), config.get("outside_gate", {})
    problems = _declaration_problems({**not_gates, **outside}, today)
    gates = [
        (file, job_id)
        for file, workflow in workflows.items()
        for job_id, job in (workflow.get("jobs") or {}).items()
        if job.get("name") == GATE
    ]
    if len(gates) != 1:
        return [*problems, f"{len(gates)} jobs are named {GATE}; exactly one must be."]
    gate_file, gate_id = gates[0]
    jobs = workflows[gate_file]["jobs"]
    problems += _gate_problems(gate_file, workflows[gate_file])
    problems += _job_problems(gate_file, gate_id, jobs, not_gates)
    problems += _outside_problems(workflows, gate_file, outside)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"fail unless every pull request job is a need of {GATE}")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    workflows = load(args.root)
    config = json.loads((args.root / CONFIG).read_text())
    problems = check(workflows, config, datetime.now(UTC).date())
    jobs = sum(len(workflow.get("jobs") or {}) for workflow in workflows.values())
    print(f"{len(workflows)} workflows, {jobs} jobs, {len(problems)} wiring problems")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
