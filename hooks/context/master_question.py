"""A master never waits on a question the operator is not there to answer: when he has not typed in the master pane for
ten minutes, the question tool is denied before it opens and each question goes to his Priorities as a ledger question."""

import hashlib
import os
import time

from hooks.context import master_coding, operator_mode
from hooks.observability.transcript import iter_complete_records

WINDOW_SEC = 600
MOVED = (
    "The operator has not typed in this pane for ten minutes, so the question tool did not open and your questions "
    "were moved to his Priorities as ledger questions: {asked} Do not ask them again. Keep working and act on his "
    "answers when they land."
)


def move(payload: dict, ledger=None, environ=None, now: float | None = None) -> str:
    """The denial naming the moved questions, or empty when the question tool may open."""
    session = payload.get("session_id")
    if payload.get("tool_name") != operator_mode.QUESTION_TOOL or not master_coding.is_master(session):
        return ""
    now = time.time() if now is None else now
    typed = operator_mode._load(session).get("turn_at")
    if typed is not None and now - typed < WINDOW_SEC:
        return ""
    questions = [q for q in (payload.get("tool_input") or {}).get("questions") or [] if q.get("question")]
    if not questions or not _deliberate(payload):
        return ""
    slug, name = operator_mode._binding(os.environ if environ is None else environ, session)
    if ledger is None:
        from scripts.swarm.ledger_client import LedgerClient

        ledger = LedgerClient()
    for question in questions:
        text = _text(question)
        ledger.question(slug, text, name, hashlib.sha256(f"{session}:{typed}:{text}".encode()).hexdigest()[:32])
    return MOVED.format(asked=" ".join(q["question"] for q in questions))


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
