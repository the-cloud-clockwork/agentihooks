from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm_ledger import plan_ranges
from scripts.swarm_ledger.ledger_phases import PLANNING_DEFAULT

PHASE_MARKER = re.compile(r"^<!--\s*phase:\s*(?P<body>.*?)\s*-->$")
TOKEN = re.compile(r'(\w+)=(".*?"|\S+)')
PHASE_KEYS = {"id", "depends", "planning", "not_duplicate"}
DESCRIPTION_LIMIT = 800


class PlanError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Normalized:
    kind: str
    title: str
    markdown: str
    content: dict
    slices: dict[str, list[str]]


def normalize(data: bytes | str, name: str, existing: Iterable[str] = ()) -> Normalized:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if not text.strip():
        raise PlanError(["the plan has no content"])
    existing_ids = set(existing)
    document = _as_json(text)
    if document is not None:
        return _from_json(document, name, existing_ids)
    return _from_markdown(text, name, existing_ids)


def _as_json(text: str) -> dict | None:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(document, dict) and isinstance(document.get("phases"), list):
        return document
    if isinstance(document, list):
        return {"phases": document}
    return None


def _from_json(document: dict, name: str, existing: set[str]) -> Normalized:
    raw_phases = document["phases"]
    if not raw_phases:
        raise PlanError(["the plan file needs a nonempty phases list"])
    title = document.get("title")
    title = title if isinstance(title, str) and title.strip() else _stem(name)
    errors: list[str] = []
    phases = [_json_phase(position, entry, errors) for position, entry in enumerate(raw_phases, 1)]
    if errors:
        raise PlanError(errors)
    errors = _dependency_errors(phases, existing)
    if errors:
        raise PlanError(errors)
    markdown = _render_markdown(title, phases)
    ranged = [{"id": phase.get("id") or f"_pos{i}", "title": phase["title"]} for i, phase in enumerate(phases, 1)]
    try:
        plan_ranges.phase_lines(markdown, ranged)
    except ValueError as exc:
        raise PlanError([str(exc)]) from None
    slices = {phase["title"]: [] for phase in phases}
    return Normalized(
        kind="json", title=title, markdown=markdown, content={"title": title, "phases": phases}, slices=slices
    )


def _json_phase(position: int, entry: Any, errors: list[str]) -> dict:
    where = f"phase {position}"
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be an object")
        return {"title": "", "description": "", "planning": PLANNING_DEFAULT}
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{where}: needs a title")
        title = ""
    description = entry.get("description", "")
    if not isinstance(description, str):
        errors.append(f"{where}: description must be a string")
        description = ""
    planning = entry.get("planning", PLANNING_DEFAULT)
    if planning not in ("manual", "auto"):
        errors.append(f"{where}: planning must be manual or auto")
    phase = {"title": title, "description": description, "planning": planning}
    depends_on = entry.get("depends_on")
    if depends_on is not None:
        valid = isinstance(depends_on, list) and all(
            isinstance(dep, (int, str)) and not isinstance(dep, bool) for dep in depends_on
        )
        if not valid:
            errors.append(f"{where}: depends_on must be a list of positions or ids")
        elif depends_on:
            phase["depends_on"] = depends_on
    phase_id = entry.get("id")
    if phase_id is not None:
        if not isinstance(phase_id, str) or not phase_id.strip():
            errors.append(f"{where}: id must be a string")
        else:
            phase["id"] = phase_id
    not_duplicate = entry.get("not_duplicate")
    if not_duplicate is not None:
        if not isinstance(not_duplicate, str) or not not_duplicate.strip():
            errors.append(f"{where}: not_duplicate must say in plain words why the phase differs")
        else:
            phase["not_duplicate"] = not_duplicate
    return phase


def _render_markdown(title: str, phases: list[dict]) -> str:
    blocks = [f"# {title}"]
    for phase in phases:
        blocks.append(f"## {phase['title']}")
        blocks.append(_marker_line(phase))
        if phase["description"]:
            blocks.append(phase["description"])
    return "\n\n".join(blocks) + "\n"


def _marker_line(phase: dict) -> str:
    parts = []
    if "id" in phase:
        parts.append(f"id={phase['id']}")
    depends_on = phase.get("depends_on")
    if depends_on and all(isinstance(dep, str) for dep in depends_on):
        parts.append(f"depends={','.join(depends_on)}")
    parts.append(f"planning={phase['planning']}")
    if "not_duplicate" in phase:
        parts.append(f'not_duplicate="{phase["not_duplicate"]}"')
    return f"<!-- phase: {' '.join(parts)} -->"


def _from_markdown(text: str, name: str, existing: set[str]) -> Normalized:
    entries = plan_ranges.sections(text)
    phase_idx = [i for i in range(len(entries)) if entries[i][1] and _has_marker(entries, i)]
    if not phase_idx:
        return _raw(text, name, entries)
    errors: list[str] = []
    if len({entries[i][1] for i in phase_idx}) > 1:
        errors.append("phase headings must all sit at one heading level")
    phases, ranged = [], []
    for i in phase_idx:
        title = entries[i][2]
        where = f"phase {title!r}"
        fields = _marker_fields(entries[i + 1][2].strip(), where, errors)
        phases.append(_marked_phase(title, fields, where, errors))
        ranged.append({"id": phases[-1].get("id") or f"_pos{len(ranged) + 1}", "title": title})
    if errors:
        raise PlanError(errors)
    try:
        ranges = plan_ranges.phase_lines(text, ranged)
    except ValueError as exc:
        raise PlanError([str(exc)]) from None
    if any(level == 0 and plan_ranges.ANCHOR.fullmatch(line) for _, level, line in entries):
        try:
            plan_ranges.require_markers(text, ranges)
        except ValueError as exc:
            raise PlanError([str(exc)]) from None
    errors = _dependency_errors(phases, existing)
    if errors:
        raise PlanError(errors)
    slices = {}
    for i, phase, row in zip(phase_idx, phases, ranged):
        rng = ranges[row["id"]]
        slices[phase["title"]] = plan_ranges.slice_anchors(text, rng)
        _, end = plan_ranges.bounds(rng)
        phase["description"] = _phase_description(entries, entries[i + 1][0], end)
    title = next((text for _, level, text in entries if level == 1), None) or _stem(name)
    return Normalized(
        kind="marked", title=title, markdown=text, content={"title": title, "phases": phases}, slices=slices
    )


def _has_marker(entries: list, i: int) -> bool:
    if i + 1 >= len(entries):
        return False
    level, line = entries[i + 1][1], entries[i + 1][2]
    return level == 0 and bool(PHASE_MARKER.fullmatch(line.strip()))


def _marker_fields(body: str, where: str, errors: list[str]) -> dict:
    fields = {}
    for key, raw in TOKEN.findall(body):
        if key not in PHASE_KEYS:
            errors.append(f"{where}: unknown key {key} in phase marker")
            continue
        fields[key] = raw[1:-1] if raw.startswith('"') else raw
    return fields


def _marked_phase(title: str, fields: dict, where: str, errors: list[str]) -> dict:
    planning = fields.get("planning", PLANNING_DEFAULT)
    if planning not in ("manual", "auto"):
        errors.append(f"{where}: planning must be manual or auto")
    phase = {"title": title, "description": "", "planning": planning}
    if fields.get("depends"):
        phase["depends_on"] = [token.strip() for token in fields["depends"].split(",") if token.strip()]
    if fields.get("id"):
        phase["id"] = fields["id"]
    if "not_duplicate" in fields:
        value = fields["not_duplicate"]
        if not value.strip():
            errors.append(f"{where}: not_duplicate must say in plain words why the phase differs")
        else:
            phase["not_duplicate"] = value
    return phase


def _phase_description(entries: list, marker_line: int, end: int) -> str:
    prose = []
    for n, level, line in entries:
        if n <= marker_line:
            continue
        if n > end:
            break
        if level or plan_ranges.ANCHOR.fullmatch(line.strip()):
            break
        prose.append(line)
    return _join(prose)


def _raw(text: str, name: str, entries: list) -> Normalized:
    title = next((entry[2] for entry in entries if entry[1] == 1), None) or _stem(name)
    phase = {"title": title, "description": _first_paragraph(entries), "planning": PLANNING_DEFAULT}
    return Normalized(
        kind="raw", title=title, markdown=text, content={"title": title, "phases": [phase]}, slices={title: []}
    )


def _first_paragraph(entries: list) -> str:
    started = None
    collected = []
    for n, level, line in entries:
        blank = not line.strip()
        if started is None:
            if level or blank:
                continue
            started = n
        elif level or blank:
            break
        collected.append(line)
    return _join(collected)


def _dependency_errors(phases: list[dict], existing: set[str]) -> list[str]:
    explicit = {phase["id"]: position for position, phase in enumerate(phases, 1) if phase.get("id")}
    graph: dict[int, list[int]] = {}
    errors = []
    for position, phase in enumerate(phases, 1):
        resolved = []
        for dep in phase.get("depends_on", []):
            if isinstance(dep, bool) or not isinstance(dep, (int, str)):
                errors.append(f"phase {position} depends_on entries must be an integer position or an id string")
            elif isinstance(dep, int):
                if 1 <= dep <= len(phases):
                    resolved.append(dep)
                else:
                    errors.append(f"phase {position} depends on position {dep}, outside the plan")
            elif dep in existing:
                continue
            elif dep in explicit:
                resolved.append(explicit[dep])
            else:
                errors.append(f"phase {position} depends on unknown phase {dep}")
        graph[position] = resolved
    if errors:
        return errors
    visited: set[int] = set()
    for node in graph:
        cycle = _cycle(node, graph, [], visited)
        if cycle:
            return [f"phase dependency cycle: {' -> '.join(str(n) for n in cycle)}"]
    return errors


def _cycle(node: int, graph: dict[int, list[int]], chain: list[int], visited: set[int]) -> list[int]:
    if node in chain:
        return [*chain[chain.index(node) :], node]
    if node in visited:
        return []
    for dep in graph[node]:
        found = _cycle(dep, graph, [*chain, node], visited)
        if found:
            return found
    visited.add(node)
    return []


def _join(lines: list[str]) -> str:
    return _bound(" ".join(line.strip() for line in lines if line.strip()))


def _bound(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    return text[: cut if cut > 0 else limit].rstrip()


def _stem(name: str) -> str:
    return Path(name).stem
