"""Closing a ledger: a Summary section built from the ledger itself, written into the overview, and when it closed."""

import re

OPS = ("summary_set", "close", "reopen")
HEAD = "Summary"
MARK = f"\n\n{HEAD}\n"
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
MAX_NOTE = 4000


def check(op):
    allowed = {"op", "id", "by", "note"} if op["op"] == "summary_set" else {"op", "id", "by"}
    if not set(op) <= allowed or not isinstance(op.get("by"), str) or not AUTHOR_RE.match(op["by"]):
        raise ValueError(f"{op['op']} takes id, by (an author name){' and note' if 'note' in allowed else ''}")
    if not isinstance(op.get("note", ""), str) or len(op.get("note", "")) > MAX_NOTE:
        raise ValueError(f"note must be text of at most {MAX_NOTE} characters")


def apply(doc, op, ctx):
    if op["op"] in ("close", "reopen"):
        doc["closed_at"] = ctx.at if op["op"] == "close" else None
        ctx.stamp("closed_at", op["by"])
        ctx.record(op["by"], "ledger closed" if op["op"] == "close" else "ledger reopened", "", id=op["id"])
        return True
    doc["overview"] = with_summary(doc.get("overview", ""), summary(doc, op.get("note", "")))
    ctx.stamp("overview", op["by"])
    ctx.record(op["by"], "summary written", "overview", id=op["id"])
    return True


def _in_scope(items):
    return [i for i in items if not i.get("out_of_scope")]


def _answered(question):
    return any(not a.get("deleted") and a.get("text", "").strip() for a in question.get("answers", []))


def _block(title, lines):
    return [f"{title}:", *(f"- {line}" for line in lines or ["none"])]


def summary(doc, note=""):
    phases, tasks = _in_scope(doc.get("phases", [])), _in_scope(doc.get("tasks", []))
    merged = [f"{t['title']} {t['pr_url']}" for t in tasks if t.get("state") == "done" and t.get("pr_url")]
    still = [f"{t['title']} ({t.get('state') or 'open'})" for t in tasks if t.get("state") != "done"]
    follow = [f["text"] for f in _in_scope(doc.get("followups", [])) if not f.get("done")]
    asked = [q["text"] for q in _in_scope(doc.get("questions", [])) if not _answered(q)]
    lines = [HEAD, *([note.strip(), ""] if note.strip() else [])]
    lines.append(f"Phases done {sum(1 for p in phases if p.get('done'))} of {len(phases)}.")
    lines += _block("Merged", merged) + _block("Tasks still open", still)
    lines += _block("Follow ups still open", follow) + _block("Questions unanswered", asked)
    return "\n".join(lines)


def intro(overview):
    return overview.split(MARK, 1)[0]


def with_summary(overview, text):
    return f"{intro(overview)}\n\n{text}"
