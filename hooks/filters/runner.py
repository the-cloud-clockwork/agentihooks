from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import PurePath

from hooks import classifier
from hooks.filters import extract, schema

PURPOSE = "filter"
YES_LINE = 0.5
TRUE = "yes, the finding goes against the intent"
FALSE = "no, the finding is fine"


@dataclass(frozen=True)
class Finding:
    where: tuple
    start: int
    end: int
    text: str
    reason: str


def _passed() -> dict:
    return {"returncode": 0, "stdout": "", "stderr": ""}


def _applies(spec: schema.FilterSpec, tool_input: dict) -> bool:
    if not spec.paths:
        return True
    path = extract.target_path(tool_input)
    name = PurePath(path).name
    return bool(path) and any(fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob) for glob in spec.paths)


def find(spec: schema.FilterSpec, pieces: list[extract.Piece]) -> list[Finding]:
    if spec.mode == "classifier":
        return [Finding(p.where, 0, len(p.text), p.text, "whole text") for p in pieces if p.text]
    return [
        Finding(piece.where, match.start(), match.end(), match.group(0), finder.reason)
        for piece in pieces
        for finder in spec.finders
        for match in finder.pattern.finditer(piece.text)
        if match.group(0)
    ]


def _intent(spec: schema.FilterSpec, tool_input: dict) -> str:
    value = tool_input.get(spec.intent_from) if spec.intent_from else None
    return value if isinstance(value, str) and value else spec.intent


def questions(spec: schema.FilterSpec, intent: str, findings: list[Finding]) -> dict:
    return {
        f"finding_{i}": classifier.YesNo(
            f"{spec.question}\nIntent: {intent}\nFinding: {finding.text}\nReason: {finding.reason}",
            true=TRUE,
            false=FALSE,
        )
        for i, finding in enumerate(findings)
    }


def confirm(entry: dict, spec: schema.FilterSpec, payload: dict, findings: list[Finding]) -> list[Finding]:
    if spec.mode == "finders":
        return findings
    tool_input = payload.get("tool_input") or {}
    intent = _intent(spec, tool_input)
    state = {
        "filter": entry["file"],
        "tool": str(payload.get("tool_name") or ""),
        "path": extract.target_path(tool_input),
        "intent": intent,
    }
    try:
        result = classifier.decide(state, questions(spec, intent, findings), purpose=PURPOSE, fallbacks=[])
    except classifier.ClassifierInputError:
        raise
    except classifier.ClassifierError as error:
        from hooks.common import log

        log(
            "filter passed: classifier unavailable",
            {"filter": entry["path"], "findings": len(findings), "error": str(error)},
        )
        return []
    return [f for i, f in enumerate(findings) if (result.answers[f"finding_{i}"].noul or 0) >= YES_LINE]


def _listing(findings: list[Finding]) -> str:
    return "\n".join(f'- "{finding.text}": {finding.reason}' for finding in findings)


def send_back(step: str, payload: dict, findings: list[Finding]) -> dict:
    return {"returncode": 2, "stdout": "", "stderr": f"filter sent the text back:\n{_listing(findings)}"}


def flag(step: str, payload: dict, findings: list[Finding]) -> dict:
    context = f"filter flagged:\n{_listing(findings)}"
    return {"returncode": 0, "stdout": json.dumps({"context": context}), "stderr": ""}


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    kept, last = [], 0
    for start, end in sorted(spans):
        if start > last:
            kept.append(text[last:start])
        last = max(last, end)
    kept.append(text[last:])
    return "".join(kept)


def strip(step: str, payload: dict, findings: list[Finding]) -> dict:
    from hooks.targets.capabilities import applies_input_rewrite

    if step != "pre" or not applies_input_rewrite():
        return send_back(step, payload, findings)
    tool_input = payload.get("tool_input") or {}
    texts = {piece.where: piece.text for piece in extract.pieces(str(payload.get("tool_name") or ""), tool_input)}
    spans: dict[tuple, list[tuple[int, int]]] = {}
    for finding in findings:
        spans.setdefault(finding.where, []).append((finding.start, finding.end))
    replaced = {where: _cut(texts[where], cut) for where, cut in spans.items()}
    decision = "allow" if payload.get("permission_mode") == "bypassPermissions" else "ask"
    out = {
        "decision": decision,
        "tool_input": extract.rewrite(tool_input, replaced),
        "context": f"filter stripped:\n{_listing(findings)}",
    }
    return {"returncode": 0, "stdout": json.dumps(out), "stderr": ""}


ACTIONS = {"send-back": send_back, "flag": flag, "strip": strip}


def run(entry: dict, step: str, payload: dict) -> dict:
    try:
        spec = schema.load(entry["path"])
    except schema.FilterSchemaError as error:
        return {"error": f"invalid filter: {error}"}
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict) or not _applies(spec, tool_input):
        return _passed()
    findings = find(spec, extract.pieces(str(payload.get("tool_name") or ""), tool_input))
    try:
        confirmed = confirm(entry, spec, payload, findings) if findings else []
    except classifier.ClassifierInputError as error:
        return {"error": f"classifier refused the questions: {error}"}
    if not confirmed:
        return _passed()
    return ACTIONS[spec.action](step, payload, confirmed)
