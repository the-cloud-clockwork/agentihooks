"""A master never waits on a question the operator is not there to answer: when he has not typed in the master pane for
ten minutes, the question tool is denied before it opens and each question goes to his Priorities as a ledger question."""

import hashlib
import os
import time

from hooks.context import master_coding, operator_mode

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
    if not questions:
        return ""
    slug, name = operator_mode._binding(os.environ if environ is None else environ, session)
    if ledger is None:
        from scripts.swarm.ledger_client import LedgerClient

        ledger = LedgerClient()
    for question in questions:
        text = _text(question)
        ledger.question(slug, text, name, hashlib.sha256(f"{session}:{typed}:{text}".encode()).hexdigest()[:32])
    return MOVED.format(asked=" ".join(q["question"] for q in questions))


def _text(question: dict) -> str:
    labels = [o["label"] for o in question.get("options") or [] if o.get("label")]
    options = f" Options: {'; '.join(labels)}." if labels else ""
    return f"{question['question']}{options}"
