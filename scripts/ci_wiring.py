import argparse
import json
import re
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml

GATE = "Gate — Required"
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target", "merge_group")
GATE_EVENTS = ("pull_request", "merge_group")
FILTERED_EVENTS = ("pull_request", "push")
FILTERS = ("paths", "paths-ignore", "types")
CONFIG = ".github/gate-wiring.json"
FIELDS = ("reason", "owner")
LAB_FIELDS = (*FIELDS, "replaces")
LAB_PREFIX = "lab/"
LAB_GATE_EVENTS = ("merge_group",)
DELIVERY = "delivery.yaml"
Declarations = dict[str, dict[str, Any]]
Wiring = TypedDict(
    "Wiring", {"not_gates": Declarations, "outside_gate": Declarations, "lab_gates": Declarations}, total=False
)
LabQueue = TypedDict("LabQueue", {"authoritative": list[str], "full_suite_entries": int}, total=False)
Lab = TypedDict("Lab", {"authoritative": list[str], "gate_required": bool, "queue": LabQueue}, total=False)
EVENT_TERM = re.compile(r"""github\.event_name\s*(==|!=)\s*(['"])(\w+)\2""")
EXPRESSION = re.compile(r"\$\{\{(.*)\}\}", re.DOTALL)


def triggers(workflow: dict[Any, Any]) -> dict[str, dict[Any, Any]]:
    on = workflow.get("on", workflow.get(True)) or cast(dict[Any, Any], {})
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        return {event: cast(dict[Any, Any], {}) for event in cast(list[Any], on)}
    return {event: config or cast(dict[Any, Any], {}) for event, config in on.items()}


def needs_of(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs") or cast(list[Any], [])
    return {needs} if isinstance(needs, str) else set(cast(list[Any], needs))


def _skips_pull_requests(term: str) -> bool:
    operands = re.split(r"(==|!=)", term)
    if len(operands) == 3 and operands[2].strip() == "github.event_name":
        term = f"github.event_name {operands[1]} {operands[0]}"
    match = EVENT_TERM.fullmatch(term.strip())
    if match is None:
        return False
    operator, _, event = match.groups()
    return event not in PULL_REQUEST_EVENTS if operator == "==" else event == "pull_request"


def on_pull_requests(job: dict[str, Any]) -> bool:
    condition = str(job.get("if")).strip()
    wrapped = EXPRESSION.fullmatch(condition)
    condition = wrapped.group(1) if wrapped else condition
    return "||" in condition or not any(_skips_pull_requests(term) for term in condition.split("&&"))


def load(root: Path) -> dict[str, dict[Any, Any]]:
    folder = root / ".github/workflows"
    paths = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    return {path.name: yaml.safe_load(path.read_text()) or cast(dict[Any, Any], {}) for path in paths}


def _gate_problems(file: str, workflow: dict[Any, Any], events: tuple[str, ...]) -> list[str]:
    on = triggers(workflow)
    problems = [f"{file} holds {GATE} but does not run on {event}." for event in events if event not in on]
    for event in FILTERED_EVENTS:
        for key in FILTERS:
            if key in on.get(event, cast(dict[Any, Any], {})):
                problems.append(f"{file} filters its {event} trigger by {key}, so a core gate skips some changes.")
    return problems


def _job_problems(
    file: str, gate_id: str, jobs: dict[str, dict[str, Any]], not_gates: dict[str, dict[str, Any]]
) -> list[str]:
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


def _outside_problems(
    workflows: dict[str, dict[Any, Any]], gate_file: str, outside: dict[str, dict[str, Any]]
) -> list[str]:
    jobs = [
        f"{file}/{job_id}"
        for file, workflow in workflows.items()
        if file != gate_file
        and any(
            event != "pull_request_target" or config.get("types") != ["dequeued"]
            for event, config in triggers(workflow).items()
            if event in PULL_REQUEST_EVENTS
        )
        for job_id, job in (workflow.get("jobs") or cast(dict[str, Any], {})).items()
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


def _declaration_problems(declared: dict[str, dict[str, Any]], fields: tuple[str, ...] = FIELDS) -> list[str]:
    return [
        f"{key} is declared without its {field}."
        for key, entry in declared.items()
        for field in fields
        if not entry.get(field)
    ]


def _lab_problems(workflows: dict[str, dict[Any, Any]], declared: dict[str, dict[str, Any]], lab: Lab) -> list[str]:
    jobs = {f"{file}/{job_id}" for file, workflow in workflows.items() for job_id in workflow.get("jobs") or ()}
    problems = _declaration_problems(declared, LAB_FIELDS)
    problems += [
        f"{key} is declared a lab gate but is not named {LAB_PREFIX}<gate>."
        for key in declared
        if not key.startswith(LAB_PREFIX) or key == LAB_PREFIX
    ]
    problems += [
        f"{key} replaces {entry['replaces']}, which is no workflow job."
        for key, entry in declared.items()
        if entry.get("replaces") and entry["replaces"] not in jobs
    ]
    listed = {
        "lab.authoritative": lab.get("authoritative", []),
        "lab.queue.authoritative": lab.get("queue", {}).get("authoritative", []),
    }
    return problems + [
        f"{key} names {gate}, but {CONFIG} declares no {LAB_PREFIX}{gate} lab gate."
        for key, gates in listed.items()
        for gate in gates
        if f"{LAB_PREFIX}{gate}" not in declared
    ]


def check(workflows: dict[str, dict[Any, Any]], config: Wiring, lab: Lab | None = None) -> list[str]:
    from scripts import ci_gate_queue

    lab = lab or Lab()
    try:
        workflows = ci_gate_queue.normalize(workflows)
    except ValueError as error:
        return [str(error)]
    not_gates, outside = (
        config.get("not_gates", cast(dict[str, Any], {})),
        config.get("outside_gate", cast(dict[str, Any], {})),
    )
    problems = _declaration_problems({**not_gates, **outside})
    problems += _lab_problems(workflows, config.get("lab_gates", {}), lab)
    gates = [
        (file, job_id)
        for file, workflow in workflows.items()
        for job_id, job in (workflow.get("jobs") or cast(dict[str, Any], {})).items()
        if job.get("name") == GATE
    ]
    if len(gates) != 1:
        return [*problems, f"{len(gates)} jobs are named {GATE}; exactly one must be."]
    gate_file, gate_id = gates[0]
    jobs = workflows[gate_file]["jobs"]
    events = LAB_GATE_EVENTS if lab.get("gate_required") else GATE_EVENTS
    problems += _gate_problems(gate_file, workflows[gate_file], events)
    problems += _job_problems(gate_file, gate_id, jobs, not_gates)
    if "feature-suite" in jobs:
        problems += [
            f"{gate_file}/feature-suite is not a need of {job_id}."
            for job_id in ("promotion-ready", "promote")
            if "feature-suite" not in needs_of(jobs.get(job_id, {}))
        ]
    problems += _outside_problems(workflows, gate_file, outside)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"fail unless every pull request job is a need of {GATE}")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    workflows = load(args.root)
    config = json.loads((args.root / CONFIG).read_text())
    delivery = args.root / DELIVERY
    lab: Lab | None = yaml.safe_load(delivery.read_text())["lab"] if delivery.is_file() else None
    problems = check(workflows, config, lab)
    jobs = sum(len(workflow.get("jobs") or cast(dict[Any, Any], {})) for workflow in workflows.values())
    print(f"{len(workflows)} workflows, {jobs} jobs, {len(problems)} wiring problems")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
