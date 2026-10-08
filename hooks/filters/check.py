from __future__ import annotations

import os
from dataclasses import dataclass

from hooks.context import conditions


@dataclass(frozen=True)
class FilterOutcome:
    decision: str
    reason: str = ""
    tool_input: dict | None = None


ALLOW = FilterOutcome("allow")


def _outcome(result: conditions.StepResult | None, tool_input: dict) -> FilterOutcome:
    if result is None:
        return ALLOW
    reason = "\n".join(result.reasons)
    if result.decision == "deny":
        return FilterOutcome("deny", reason)
    if result.input_patch:
        return FilterOutcome("rewrite", reason, {**tool_input, **result.input_patch})
    if result.decision == "ask":
        return FilterOutcome("deny", reason)
    return ALLOW


def check(tool: str, tool_input: dict, cwd: str | None = None) -> FilterOutcome:
    try:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "",
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": cwd or os.getcwd(),
        }
        return _outcome(conditions.run_step("pre", payload), tool_input)
    except Exception as error:
        from hooks.common import log

        log("filter check failed open", {"tool": tool, "error": str(error)})
        return ALLOW


def screen(tool: str, text: str) -> str:
    outcome = check(tool, {"text": text})
    if outcome.decision == "deny":
        raise ValueError(outcome.reason)
    if outcome.decision != "rewrite":
        return text
    rewritten = outcome.tool_input["text"]
    if not rewritten.strip():
        raise ValueError("a filter stripped all of the text")
    return rewritten
