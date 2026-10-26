from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm_ledger import ledger_phases, plan_ranges
from scripts.swarm_ledger.ledger_phases import PLANNING_DEFAULT

PHASE_MARKER = re.compile(r"^<!--\s*phase:\s*(?P<body>.*?)\s*-->$")
TOKEN = re.compile(r'(\w+)=(?:"(.*?)"|(\S+))')
PHASE_KEYS = {"id", "depends", "planning", "not_duplicate"}
DESCRIPTION_LIMIT = 800
ID_RULE = "id must start with a letter and hold only letters, digits, dots, dashes or underscores"
NOT_DUPLICATE_RULE = "not_duplicate must say in plain words why the phase differs"


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
    try:
        text = data.decode() if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        raise PlanError(["the plan is not UTF-8 text"]) from None
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
    if isinstance(document, list):
        return {"phases": document}
    if not isinstance(document, dict):
        return None
    if not isinstance(document.get("phases"), list):
        raise PlanError(["a JSON plan needs a phases list"])
    return document


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
    ranged = [{"id": phase["title"], "title": phase["title"]} for phase in phases]
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
        return {}
    phase = {
        "title": _json_title(entry, where, errors),
        "description": _json_description(entry, where, errors),
        "planning": _json_planning(entry, where, errors),
    }
    _json_depends_on(entry, phase, where, errors)
    _json_id(entry, phase, where, errors)
    _json_not_duplicate(entry, phase, where, errors)
    return phase


def _json_title(entry: dict, where: str, errors: list[str]) -> str:
    title = entry.get("title")
    if not (isinstance(title, str) and title.strip() and "\n" not in title):
        errors.append(f"{where}: needs a one line title")
    return title


def _json_description(entry: dict, where: str, errors: list[str]) -> str:
    description = entry.get("description", "")
    if not isinstance(description, str):
        errors.append(f"{where}: description must be a string")
    elif any(
        level or plan_ranges.ANCHOR.fullmatch(line) or PHASE_MARKER.fullmatch(line.strip())
        for _, level, line in plan_ranges.sections(description)
    ):
        errors.append(f"{where}: description must not hold headings or plan markers")
    return description


def _json_planning(entry: dict, where: str, errors: list[str]) -> str:
    planning = entry.get("planning", PLANNING_DEFAULT)
    if planning not in ("manual", "auto"):
        errors.append(f"{where}: planning must be manual or auto")
    return planning


def _json_depends_on(entry: dict, phase: dict, where: str, errors: list[str]) -> None:
    depends_on = entry.get("depends_on")
    if depends_on is None:
        return
    valid = isinstance(depends_on, list) and all(
        isinstance(dep, (int, str)) and not isinstance(dep, bool) for dep in depends_on
    )
    if not valid:
        errors.append(f"{where}: depends_on must be a list of positions or ids")
    elif depends_on:
        phase["depends_on"] = depends_on


def _json_id(entry: dict, phase: dict, where: str, errors: list[str]) -> None:
    phase_id = entry.get("id")
    if phase_id is None:
        return
    if isinstance(phase_id, str) and ledger_phases.ID_RE.fullmatch(phase_id):
        phase["id"] = phase_id
    else:
        errors.append(f"{where}: {ID_RULE}")


def _json_not_duplicate(entry: dict, phase: dict, where: str, errors: list[str]) -> None:
    not_duplicate = entry.get("not_duplicate")
    if not_duplicate is None:
        return
    if isinstance(not_duplicate, str) and _plain_reason(not_duplicate):
        phase["not_duplicate"] = not_duplicate
    else:
        errors.append(f"{where}: {NOT_DUPLICATE_RULE}")


def _plain_reason(value: str) -> bool:
    return bool(value.strip()) and not re.search(r'"|-->|\n', value)


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
    if phase.get("depends_on"):
        parts.append(f"depends={','.join(str(dep) for dep in phase['depends_on'])}")
    parts.append(f"planning={phase['planning']}")
    if "not_duplicate" in phase:
        parts.append(f'not_duplicate="{phase["not_duplicate"]}"')
    return f"<!-- phase: {' '.join(parts)} -->"


def _from_markdown(text: str, name: str, existing: set[str]) -> Normalized:
    if text.lstrip().partition("\n")[0].strip() == "---":
        raise PlanError(["the plan must not start with YAML front matter"])
    entries = plan_ranges.sections(text)
    markers = {i: j for i in range(len(entries)) if entries[i][1] and (j := _marker_at(entries, i)) is not None}
    names = _slice_names(entries, bool(markers))
    if not markers:
        return _raw(text, name, entries)
    errors: list[str] = []
    if len({entries[i][1] for i in markers}) > 1:
        errors.append("phase headings must all sit at one heading level")
    phases, ranged = [], []
    for i, j in markers.items():
        title = entries[i][2]
        where = f"phase {title!r}"
        fields = _marker_fields(entries[j][2].strip(), where, errors)
        phases.append(_marked_phase(title, fields, where, errors))
        ranged.append({"id": str(i), "title": title})
    if errors:
        raise PlanError(errors)
    try:
        ranges = plan_ranges.phase_lines(text, ranged)
    except ValueError as exc:
        raise PlanError([str(exc)]) from None
    if names:
        try:
            plan_ranges.require_markers(text, ranges)
        except ValueError as exc:
            raise PlanError([str(exc)]) from None
    errors = _dependency_errors(phases, existing)
    if errors:
        raise PlanError(errors)
    slices = {}
    for j, phase, row in zip(markers.values(), phases, ranged):
        slices[phase["title"]] = _phase_slices(text, entries, phase, entries[j][0], ranges[row["id"]], errors)
    if errors:
        raise PlanError(errors)
    title = next((text for _, level, text in entries if level == 1), None) or _stem(name)
    return Normalized(
        kind="marked", title=title, markdown=text, content={"title": title, "phases": phases}, slices=slices
    )


def _marker_at(entries: list, i: int) -> int | None:
    j = next((j for j in range(i + 1, len(entries)) if entries[j][1] or entries[j][2].strip()), None)
    if j is not None and not entries[j][1] and PHASE_MARKER.fullmatch(entries[j][2].strip()):
        return j
    return None


def _slice_names(entries: list, marked: bool) -> list[str]:
    names = [match[1] for _, level, line in entries if not level and (match := plan_ranges.ANCHOR.fullmatch(line))]
    if names and not marked:
        raise PlanError(["slice markers need phase markers above them"])
    if repeated := sorted({name for name in names if names.count(name) > 1}):
        raise PlanError([f"each slice marker needs its own name in the file: {', '.join(repeated)}"])
    return names


def _phase_slices(text: str, entries: list, phase: dict, marker_line: int, rng: str, errors: list[str]) -> list[str]:
    names = plan_ranges.slice_anchors(text, rng)
    start, end = plan_ranges.bounds(rng)
    if not names and any(level for n, level, _ in entries if start < n <= end):
        errors.append(f"phase {phase['title']!r} has no slice markers, so it holds prose only")
    phase["description"] = _phase_description(entries, marker_line, end)
    return names


def _marker_fields(body: str, where: str, errors: list[str]) -> dict:
    fields = {}
    for key, quoted, bare in TOKEN.findall(body):
        if key not in PHASE_KEYS:
            errors.append(f"{where}: unknown key {key} in phase marker")
            continue
        fields[key] = quoted or bare
    return fields


def _marked_phase(title: str, fields: dict, where: str, errors: list[str]) -> dict:
    planning = fields.get("planning", PLANNING_DEFAULT)
    if planning not in ("manual", "auto"):
        errors.append(f"{where}: planning must be manual or auto")
    phase = {"title": title, "planning": planning}
    if fields.get("depends"):
        tokens = [token.strip() for token in fields["depends"].split(",") if token.strip()]
        phase["depends_on"] = [int(token) if token.isdecimal() else token for token in tokens]
    if fields.get("id"):
        if ledger_phases.ID_RE.fullmatch(fields["id"]):
            phase["id"] = fields["id"]
        else:
            errors.append(f"{where}: {ID_RULE}")
    if "not_duplicate" in fields:
        value = fields["not_duplicate"]
        if not _plain_reason(value):
            errors.append(f"{where}: {NOT_DUPLICATE_RULE}")
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
    ids = [phase["id"] for phase in phases if phase.get("id")]
    if repeated := sorted({phase_id for phase_id in ids if ids.count(phase_id) > 1}):
        return [f"each phase needs its own id: {', '.join(repeated)}"]
    explicit = {phase["id"]: position for position, phase in enumerate(phases, 1) if phase.get("id")}
    graph: dict[int, list[int]] = {}
    errors = []
    for position, phase in enumerate(phases, 1):
        resolved = []
        for dep in phase.get("depends_on", []):
            if isinstance(dep, int):
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
    try:
        ledger_phases.validate([{"id": str(n), "depends_on": [str(dep) for dep in deps]} for n, deps in graph.items()])
    except ValueError as exc:
        return [str(exc)]
    return []


def _join(lines: list[str]) -> str:
    return _bound(" ".join(line.strip() for line in lines if line.strip()))


def _bound(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head, space, _ = text[: limit + 1].rpartition(" ")
    return head if space else text[:limit]


def _stem(name: str) -> str:
    return Path(name).stem
