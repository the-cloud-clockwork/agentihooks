"""The fixed workflow and stage primitives: defaults, validation and block-style YAML parsing."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

import yaml

DOCUMENT_KEYS = ("workflow", "stages")
WORKFLOW_REQUIRED = ("name", "intent", "head", "exit")
WORKFLOW_DEFAULTS: dict[str, Any] = {"max_iterations": 3, "on_exhausted": "block"}
WORKFLOW_KEYS = ("name", "intent", "head", "max_iterations", "exit", "on_exhausted")
OUTCOMES = ("pass", "retry", "enhance")
STAGE_REQUIRED = ("profile",)
STAGE_DEFAULTS: dict[str, Any] = {
    "tier": "high",
    "effort": "medium",
    "count": 1,
    "input": [],
    "filters": [],
    "iterations": 1,
    "exit": None,
    "next": dict.fromkeys(OUTCOMES),
}
STAGE_KEYS = ("profile", *STAGE_DEFAULTS)
TIERS = ("high", "low")
ON_EXHAUSTED = ("block", "escalate", "keep-best")
CASE = "case"
EXIT = "exit"
STAGE_PASS = re.compile(r"^([A-Za-z0-9_-]+)\.pass$")
TEXT_KEYS = {"workflow": ("name", "intent", "head", "exit"), "stage": ("profile", "effort")}
COUNT_KEYS = {"workflow": ("max_iterations",), "stage": ("count", "iterations")}
LIST_KEYS = ("input", "filters")


class WorkflowError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Stage:
    id: str
    profile: str
    tier: str
    effort: str
    count: int
    input: tuple[str, ...]
    filters: tuple[str, ...]
    iterations: int
    exit: str | None
    next: dict[str, str | None]

    def to_document(self) -> dict:
        return {
            "profile": self.profile,
            "tier": self.tier,
            "effort": self.effort,
            "count": self.count,
            "input": list(self.input),
            "filters": list(self.filters),
            "iterations": self.iterations,
            "exit": self.exit,
            "next": dict(self.next),
        }


@dataclass(frozen=True)
class Workflow:
    name: str
    intent: str
    head: str
    max_iterations: int
    exit: str
    on_exhausted: str
    stages: dict[str, Stage]

    def to_document(self) -> dict:
        return {
            "workflow": {key: getattr(self, key) for key in WORKFLOW_KEYS},
            "stages": {stage_id: stage.to_document() for stage_id, stage in self.stages.items()},
        }


def parse(text: str) -> Workflow:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError([f"not YAML: {exc}"]) from exc
    errors = flow_errors(root)
    if errors:
        raise WorkflowError(errors)
    return validate(document)


def flow_errors(node: yaml.Node | None) -> list[str]:
    if node is None:
        return []
    errors = []
    flow_list = isinstance(node, yaml.SequenceNode) and bool(node.value)
    if getattr(node, "flow_style", False) and (flow_list or isinstance(node, yaml.MappingNode)):
        errors.append(f"line {node.start_mark.line + 1}: flow style is refused; write block style")
    children = node.value if isinstance(node.value, list) else []
    for child in children:
        for item in child if isinstance(child, tuple) else (child,):
            errors.extend(flow_errors(item))
    return errors


def validate(document: Any) -> Workflow:
    if not isinstance(document, dict):
        raise WorkflowError(["a workflow file is a mapping with workflow and stages"])
    errors = unknown_keys(document, DOCUMENT_KEYS, "file")
    head = filled(mapping(document, "workflow", errors), WORKFLOW_REQUIRED, WORKFLOW_DEFAULTS, "workflow", errors)
    check_values(head, "workflow", "workflow", errors)
    stages = {}
    for stage_id, body in mapping(document, "stages", errors).items():
        stages[stage_id] = stage_fields(stage_id, body, errors)
    errors.extend(graph_errors(head, stages))
    if errors:
        raise WorkflowError(errors)
    built = {stage_id: build_stage(stage_id, body) for stage_id, body in stages.items()}
    return Workflow(**{key: head[key] for key in WORKFLOW_KEYS}, stages=built)


def unknown_keys(body: dict, allowed: tuple[str, ...], where: str) -> list[str]:
    return [f"{where}: unknown key {key}" for key in body if key not in allowed]


def mapping(document: dict, key: str, errors: list[str]) -> dict:
    value = document.get(key)
    if isinstance(value, dict):
        return value
    errors.append(f"{key}: must be a mapping" if key in document else f"file: missing required key {key}")
    return {}


def filled(body: dict, required: tuple[str, ...], defaults: dict, where: str, errors: list[str]) -> dict:
    errors.extend(unknown_keys(body, (*required, *defaults), where))
    errors.extend(f"{where}: missing required key {key}" for key in required if key not in body)
    errors.extend(f"{where}: {key} must not be empty" for key in required if key in body and body[key] is None)
    out = copy.deepcopy(defaults)
    out.update({key: None for key in required})
    out.update(body)
    return out


def stage_fields(stage_id: Any, body: Any, errors: list[str]) -> dict:
    where = f"stages.{stage_id}"
    if not isinstance(stage_id, str):
        errors.append(f"{where}: a stage id must be text")
    if not isinstance(body, dict):
        errors.append(f"{where}: must be a mapping")
        body = {}
    fields = filled(body, STAGE_REQUIRED, STAGE_DEFAULTS, where, errors)
    check_values(fields, "stage", where, errors)
    if isinstance(fields["next"], dict):
        fields["next"] = filled(fields["next"], (), dict.fromkeys(OUTCOMES), f"{where}.next", errors)
    else:
        errors.append(f"{where}.next: must be a mapping of pass, retry and enhance")
        fields["next"] = dict.fromkeys(OUTCOMES)
    return fields


def check_values(fields: dict, level: str, where: str, errors: list[str]) -> None:
    for key in TEXT_KEYS[level]:
        if key in fields and fields[key] is not None and not isinstance(fields[key], str):
            errors.append(f"{where}.{key}: must be text")
    for key in COUNT_KEYS[level]:
        value = fields[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"{where}.{key}: must be a positive integer")
    errors.extend(choice_errors(fields, level, where))
    if level == "stage":
        errors.extend(stage_value_errors(fields, where))


def choice_errors(fields: dict, level: str, where: str) -> list[str]:
    key, allowed = ("on_exhausted", ON_EXHAUSTED) if level == "workflow" else ("tier", TIERS)
    if fields[key] in allowed:
        return []
    return [f"{where}.{key}: {fields[key]} is not one of {', '.join(allowed)}"]


def stage_value_errors(fields: dict, where: str) -> list[str]:
    errors = []
    for key in LIST_KEYS:
        value = fields[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{where}.{key}: must be a list of names")
            fields[key] = []
    if fields["exit"] is not None and not isinstance(fields["exit"], str):
        errors.append(f"{where}.exit: must be text or null")
    return errors


def graph_errors(head: dict, stages: dict[str, dict]) -> list[str]:
    errors = []
    if isinstance(head["head"], str) and head["head"] not in stages:
        errors.append(f"workflow.head: {head['head']} is not a stage")
    match = STAGE_PASS.match(head["exit"]) if isinstance(head["exit"], str) else None
    if match and match.group(1) not in stages:
        errors.append(f"workflow.exit: {match.group(1)} is not a stage")
    for stage_id, fields in stages.items():
        errors.extend(
            f"stages.{stage_id}.input: {item} is not case or a stage"
            for item in fields["input"]
            if item != CASE and item not in stages
        )
        errors.extend(
            f"stages.{stage_id}.next.{outcome}: {target} is not a stage, exit or null"
            for outcome, target in fields["next"].items()
            if outcome in OUTCOMES and target not in (None, EXIT) and not (isinstance(target, str) and target in stages)
        )
    return errors


def build_stage(stage_id: str, fields: dict) -> Stage:
    return Stage(
        id=stage_id,
        profile=fields["profile"],
        tier=fields["tier"],
        effort=fields["effort"],
        count=fields["count"],
        input=tuple(fields["input"]),
        filters=tuple(fields["filters"]),
        iterations=fields["iterations"],
        exit=fields["exit"],
        next={outcome: fields["next"][outcome] for outcome in OUTCOMES},
    )
