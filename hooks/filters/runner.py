from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, replace
from pathlib import PurePath

from hooks import classifier
from hooks.classifier import definitions, runner
from hooks.filters import extract, rounds, schema
from hooks.filters.finders import scripts

PURPOSE = "filter"


@dataclass(frozen=True)
class Finding:
    where: tuple
    start: int
    end: int
    text: str
    reason: str
    context: str = ""


def _passed() -> dict:
    return {"returncode": 0, "stdout": "", "stderr": ""}


def _applies(spec: schema.FilterSpec, tool_input: dict) -> bool:
    if not spec.paths:
        return True
    path = extract.target_path(tool_input)
    name = PurePath(path).name
    return bool(path) and any(fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob) for glob in spec.paths)


def find(spec: schema.FilterSpec, pieces: list[extract.Piece], payload: dict | None = None) -> list[Finding]:
    payload = payload or {}
    if spec.mode == "classifier":
        return [Finding(p.where, 0, len(p.text), p.text, "whole text", p.text) for p in pieces if p.text]
    named = scripts.resolve(payload.get("cwd")) if any(f.script for f in spec.finders) else {}
    path = extract.target_path(payload.get("tool_input") or {})
    tool = payload.get("tool_name", "")
    findings = []
    for piece in pieces:
        for finder in spec.finders:
            if finder.script:
                findings.extend(
                    Finding(piece.where, **span) for span in scripts.run(finder.script, named, piece.text, path, tool)
                )
            else:
                findings.extend(
                    Finding(piece.where, match.start(), match.end(), match.group(0), finder.reason)
                    for match in finder.pattern.finditer(piece.text)
                    if match.group(0)
                )
    sources = {piece.where: piece.text for piece in pieces}
    return [
        replace(finding, context=_enclosing_lines(sources[finding.where], finding.start, finding.end))
        for finding in findings
    ]


def _enclosing_lines(text: str, start: int, end: int) -> str:
    first = text[:start].rfind("\n") + 1
    last = text.find("\n", end - 1)
    return text[first : len(text) if last < 0 else last]


def _intent(spec: schema.FilterSpec, tool_input: dict) -> str:
    value = tool_input.get(spec.intent_from) if spec.intent_from else None
    return value if isinstance(value, str) and value else spec.intent


def _params(spec: schema.FilterSpec, intent: str, findings: list[Finding]) -> dict:
    return {
        "question": spec.question,
        "intent": intent,
        "findings": [{"text": f.text, "reason": f.reason, "context": f.context} for f in findings],
    }


def confirm(entry: dict, spec: schema.FilterSpec, payload: dict, findings: list[Finding]) -> list[Finding]:
    if spec.mode == "finders":
        return findings
    tool_input = payload.get("tool_input") or {}
    intent = _intent(spec, tool_input)
    state = {
        "filter": entry["file"],
        "tool": payload.get("tool_name"),
        "path": extract.target_path(tool_input),
        "intent": intent,
    }
    try:
        output = runner.run(PURPOSE, state, _params(spec, intent, findings), decider=classifier.decide)
    except classifier.ClassifierInputError:
        raise
    except classifier.ClassifierError as error:
        from hooks.common import log

        log(
            "filter passed: classifier unavailable",
            {"filter": entry["path"], "findings": len(findings), "error": str(error)},
        )
        return []
    return [f for i, f in enumerate(findings) if output.verdicts[f"finding_{i}"]]


def _listing(findings: list[Finding]) -> str:
    return "\n".join(f'- "{finding.text}": {finding.reason}' for finding in findings)


def send_back(findings: list[Finding]) -> dict:
    return {"returncode": 2, "stdout": "", "stderr": f"filter sent the text back:\n{_listing(findings)}"}


def flag(findings: list[Finding]) -> dict:
    return {"returncode": 0, "stdout": json.dumps({"context": f"filter flagged:\n{_listing(findings)}"})}


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    kept, last = [], 0
    for start, end in sorted(spans):
        kept.append(text[last:start])
        last = max(last, end)
    kept.append(text[last:])
    return "".join(kept)


def strip(step: str, payload: dict, findings: list[Finding]) -> dict:
    from hooks.targets.capabilities import applies_input_rewrite

    if step != "pre" or not applies_input_rewrite():
        return send_back(findings)
    tool_input = payload.get("tool_input") or {}
    texts = {piece.where: piece.text for piece in extract.pieces(payload.get("tool_name"), tool_input)}
    spans: dict[tuple, list[tuple[int, int]]] = {}
    for finding in findings:
        spans.setdefault(finding.where, []).append((finding.start, finding.end))
    replaced = {where: _cut(texts[where], cut) for where, cut in spans.items()}
    out = {
        "tool_input": extract.rewrite(tool_input, replaced),
        "context": f"filter stripped:\n{_listing(findings)}",
    }
    if payload.get("permission_mode") != "bypassPermissions":
        out["decision"] = "ask"
    return {"returncode": 0, "stdout": json.dumps(out)}


ACTIONS = {"send-back": send_back, "flag": flag}


def _comment_text(entry: dict, findings: list[Finding], count: int) -> str:
    from scripts.swarm_ledger.ledger_comments import LIMITS, RULES

    name = entry["file"].removesuffix(".filter.yaml")
    text = f"Filter {name} passed after {count} send backs. Findings: "
    text += " ".join(f"{finding.text}: {finding.reason}." for finding in findings)
    for _, pattern in RULES:
        text = pattern.sub("flagged text", text)
    text = text.replace("-", " ").replace("(", " ").replace(";", " ")
    return " ".join(text.split()[: LIMITS["comment"]])


def _round_comment(entry: dict, findings: list[Finding], count: int) -> None:
    from hooks.common import log
    from scripts.swarm.ledger_client import LedgerClient

    slug = os.environ.get("AGENTIHOOKS_SWARM")
    task = os.environ.get("AGENTIHOOKS_SWARM_TASK")
    if slug and task:
        try:
            LedgerClient().comment(slug, task, _comment_text(entry, findings, count), by="swarm")
        except Exception as error:
            log("filter ledger comment failed", {"error": str(error)})


def _round_flag(entry: dict, payload: dict, target: str, findings: list[Finding], count: int) -> str:
    from hooks.common import log

    context = f"filter flagged: passed after {count} send-backs\n{_listing(findings)}"
    log(
        "filter flagged: round cap reached",
        {
            "filter": entry["path"],
            "session_id": rounds.session(payload),
            "target": target,
            "rounds": count,
            "findings": [{"text": f.text, "reason": f.reason} for f in findings],
        },
    )
    _round_comment(entry, findings, count)
    return context


def _limited(entry: dict, spec: schema.FilterSpec, payload: dict, findings: list[Finding]) -> dict:
    groups = {}
    for finding in findings:
        groups.setdefault(rounds.target(payload, finding.where), []).append(finding)
    denied = []
    contexts = []
    for target, group in groups.items():
        count = rounds.send_back(entry, payload, target, spec.max_rounds)
        if count >= spec.max_rounds:
            contexts.append(_round_flag(entry, payload, target, group, count))
        else:
            denied.extend(group)
    result = send_back(denied) if denied else _passed()
    if contexts:
        context = "\n".join(contexts)
        result["stdout"] = json.dumps({"context": context})
        if denied:
            result["stderr"] += f"\n{context}"
    return result


def run(entry: dict, step: str, payload: dict) -> dict:
    try:
        spec = schema.load(entry["path"])
    except schema.FilterSchemaError as error:
        return {"error": f"invalid filter: {error}"}
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict) or not _applies(spec, tool_input):
        return _passed()
    pieces = extract.pieces(payload.get("tool_name"), tool_input)
    findings = find(spec, pieces, payload)
    try:
        confirmed = confirm(entry, spec, payload, findings) if findings else []
    except classifier.ClassifierInputError as error:
        return {"error": f"classifier refused the questions: {error}"}
    targets = {rounds.target(payload, piece.where) for piece in pieces}
    dirty = {rounds.target(payload, finding.where) for finding in confirmed}
    for target in targets - dirty:
        rounds.reset(entry, payload, target)
    if not confirmed:
        return _passed()
    if spec.action == "strip":
        result = strip(step, payload, confirmed)
        return _limited(entry, spec, payload, confirmed) if result["returncode"] == 2 else result
    if spec.action == "send-back":
        return _limited(entry, spec, payload, confirmed)
    return ACTIONS[spec.action](confirmed)


def __getattr__(name):
    if name in ("TRUE", "FALSE"):
        question = definitions.load(PURPOSE).questions[0].question
        return question.true if name == "TRUE" else question.false
    raise AttributeError(name)
