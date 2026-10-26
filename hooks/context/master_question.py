"""A master never waits on a question the operator is not there to answer: when he has not typed in the master pane for
ten minutes, the question tool is denied before it opens and each question goes to his Priorities as a ledger question."""

import hashlib
import os
import time

from hooks.context import master_coding, operator_mode
from hooks.observability.transcript import iter_complete_records

WINDOW_SEC = 600
GATE = "question"
MOVED = (
    "The operator has not typed in this pane for ten minutes, so the question tool did not open and your questions "
    "were moved to his Priorities as ledger questions: {asked} Do not ask them again. Keep working and act on his "
    "answers when they land."
)


def move(payload: dict, ledger=None, environ=None, now: float | None = None, home=None) -> str:
    """The denial naming the moved questions, or empty when the question tool may open; each decision is a gate log
    row naming the operator's last typed turn it read."""
    session = payload.get("session_id")
    if payload.get("tool_name") != operator_mode.QUESTION_TOOL or not master_coding.is_master(session):
        return ""
    now = time.time() if now is None else now
    slug, name = operator_mode._binding(os.environ if environ is None else environ, session)
    typed = operator_mode._load(session).get("turn_at")
    if typed is not None and now - typed < WINDOW_SEC:
        _record(slug, name, "count", f"opened: the operator typed {_ago(typed, now)}", now, home)
        return ""
    questions = [q for q in (payload.get("tool_input") or {}).get("questions") or [] if q.get("question")]
    if not questions or not _deliberate(payload):
        _record(slug, name, "count", f"left to the operator presence check: typed {_ago(typed, now)}", now, home)
        return ""
    if ledger is None:
        from scripts.swarm.ledger_client import LedgerClient

        ledger = LedgerClient()
    for question in questions:
        text = _text(question)
        ledger.question(slug, text, name, hashlib.sha256(f"{session}:{typed}:{text}".encode()).hexdigest()[:32])
    _record(slug, name, "deny", f"moved {len(questions)} questions: the operator typed {_ago(typed, now)}", now, home)
    return MOVED.format(asked=" ".join(q["question"] for q in questions))


def _ago(typed: float | None, now: float) -> str:
    if typed is None:
        return "never since his last turn ended"
    return f"at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(typed))}, {int(now - typed)} seconds ago"


def _record(slug: str, name: str, kind: str, reason: str, now: float, home=None) -> None:
    from scripts.gates import log
    from scripts.gates.base import Who

    if slug != "<slug>":
        row = log.Row.of(
            GATE, kind, Who(name=name, task="master"), operator_mode.QUESTION_TOOL, reason, int(now * 1000)
        )
        log.append(slug, row, home)


def _deliberate(payload: dict) -> bool:
    transcript = payload.get("transcript_path")
    if not transcript or not payload.get("tool_use_id"):
        return False
    try:
        for record, _ in iter_complete_records(transcript, 0):
            if (
                record is not None
                and record.get("type") == "assistant"
                and record.get("sessionId") == payload["session_id"]
                and record.get("isSidechain") is False
                and _matches_call(record.get("message") or {}, payload)
            ):
                return True
    except OSError:
        return False
    return False


def _matches_call(message: dict, payload: dict) -> bool:
    if message.get("role") != "assistant":
        return False
    return any(
        block.get("type") == "tool_use"
        and block.get("id") == payload["tool_use_id"]
        and block.get("name") == payload["tool_name"]
        and block.get("input") == payload["tool_input"]
        for block in message.get("content") or []
        if isinstance(block, dict)
    )


def _text(question: dict) -> str:
    labels = [o["label"] for o in question.get("options") or [] if o.get("label")]
    options = f" Options: {'; '.join(labels)}." if labels else ""
    return f"{question['question']}{options}"
