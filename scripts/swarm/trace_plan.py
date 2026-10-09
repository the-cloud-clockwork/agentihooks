"""Trace plan: the classifier traces each piece of a task's plan to the task, its phase and the project intent.

plan.md in the task work folder holds one piece per line, `- what | area, area | why`. A piece the classifier finds
off intent is cut and, once the plan passes, filed as a ledger follow up. A plan with more than half its pieces cut,
or sized above one pull request, fails whole: it returns to the planner once, then the task is blocked. The verdict
lands in plan-verdict.json beside the plan. With no classifier answer the verdict is unchecked, counted as fail-open.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from hooks.classifier import ClassifierError, code_rules, decide, definitions, runner
from scripts.gates import log as gate_log
from scripts.swarm.slice_screen import ONE_PR, levels
from scripts.swarm_ledger import ledger_comments

PLAN = "plan.md"
VERDICT = "plan-verdict.json"
PURPOSE = "trace-plan"
FORMAT = "one piece per line: - what | area, area | why"
MAX_PIECES = 100
RETURNS = 1
PASS, FAIL, UNCHECKED = "pass", "fail", "unchecked"
NO_ANSWER = "the classifier did not answer"
FAIL_KINDS = {"enforce": "deny", "observe": "observe"}
CLEARANCE = PurePosixPath("mutation-cleared.txt")
CLEARANCE_FOLDER = PurePosixPath("mutation-clearances")


@dataclass(frozen=True)
class TracePlan:
    name: str = "trace-plan"
    default_mode: str = "observe"


GATE = TracePlan()


@dataclass(frozen=True)
class Piece:
    what: str
    areas: tuple
    why: str

    @property
    def key(self):
        return " | ".join((self.what, ", ".join(self.areas), self.why))


def followup_text(task_id, what):
    return f"Cut from the plan of task {task_id}: {what}"


def _piece(line, number, task_id, task_ids):
    parts = [part.strip() for part in line[2:].split("|")]
    areas = tuple(area for area in (a.strip() for a in parts[1].split(",")) if area) if len(parts) == 3 else ()
    if len(parts) != 3 or not (parts[0] and areas and parts[2]):
        raise ValueError(f"plan line {number} is not a piece; write {FORMAT}")
    piece = Piece(parts[0], areas, parts[2])
    found = ledger_comments.problems(followup_text(task_id, piece.what), "item", task_ids=task_ids)
    if found:
        raise ValueError(
            f"plan line {number}: the ledger would refuse its follow up, write what in plain words: {'; '.join(found)}"
        )
    return piece


def parse(text, task_id, task_ids=()):
    pieces = [
        _piece(line, n, task_id, task_ids) for n, line in enumerate(text.splitlines(), 1) if line.startswith("- ")
    ]
    if not pieces:
        raise ValueError(f"the plan holds no pieces; write {FORMAT}")
    if len(pieces) > MAX_PIECES:
        raise ValueError(f"the plan holds {len(pieces)} pieces, at most {MAX_PIECES}")
    return pieces


def plan_hash(pieces):
    return hashlib.sha256("\n".join(piece.key for piece in pieces).encode()).hexdigest()


def intent(doc, task_id):
    task = next((t for t in doc.get("tasks", []) if t.get("id") == task_id), {})
    phase = next((p for p in doc.get("phases", []) if p.get("id") == task.get("phase")), {})
    return {
        "project intent": doc.get("overview", ""),
        "phase": phase.get("title", ""),
        "phase intent": phase.get("description", ""),
        "task": task.get("title", ""),
        "task intent": task.get("description", ""),
        "task ids": [t["id"] for t in doc.get("tasks", [])],
    }


def _row(piece, probability, off_intent):
    ruling = all(is_clearance(area) for area in piece.areas)
    kept = ruling or probability is None or probability >= off_intent
    return {"what": piece.what, "areas": list(piece.areas), "why": piece.why, "probability": probability, "kept": kept}


def _row_key(row):
    return Piece(row["what"], tuple(row["areas"]), row["why"]).key


def _known(previous, pieces):
    rows = previous.get("pieces") or []
    if previous.get("verdict") != PASS or len(rows) >= len(pieces):
        return []
    if [_row_key(row) for row in rows] != [piece.key for piece in pieces[: len(rows)]]:
        return []
    return list(rows)


def _params(fresh, start, sized):
    return {
        "pieces": [{"slot": start + i, "number": start + i + 1, "what": piece.what} for i, piece in enumerate(fresh)],
        "sized": [{}] if sized else [],
    }


def _size(answer, sizes):
    level = round(answer.score)
    return {"level": level, "name": sizes[level], "confidence": answer.confidence}


def failures(rows, size, too_big):
    reasons = []
    cut = sum(1 for row in rows if not row["kept"])
    if cut * 2 > len(rows):
        reasons.append(f"{cut} of {len(rows)} pieces are off the task intent, more than half")
    if size["level"] > ONE_PR and size["confidence"] >= too_big:
        reasons.append(
            f"the plan is sized {size['name']} at confidence {size['confidence']:.2f}, above one pull request"
        )
    return reasons


def _verdicts(definition, state, params, answers):
    off_intent = definition.thresholds["off_intent"]
    rows = [{"kept": answers[f"piece_{piece['slot']}"].noul >= off_intent} for piece in params["pieces"]]
    size = _size(answers["size"], levels(definition))
    return {"verdict": FAIL if failures(rows, size, definition.thresholds["too_big_confidence"]) else PASS}


RULE = code_rules.CodeRule(code_rules.asked, _verdicts, {"verdict": (PASS, FAIL)}, {"verdict": FAIL})


def is_clearance(area: str) -> bool:
    path = PurePosixPath(area)
    return path == CLEARANCE or path == CLEARANCE_FOLDER or CLEARANCE_FOLDER in path.parents


def _source_areas(areas: tuple) -> list[str]:
    return [area for area in areas if PurePosixPath(area).parts[:1] != ("tests",) and not is_clearance(area)]


def trace(pieces, state, previous, now_ms=None):
    previous = previous or {}
    known = _known(previous, pieces)
    start, fresh = len(known), pieces[len(known) :]
    wire = [
        {"piece": start + i + 1, "what": p.what, "areas": _source_areas(p.areas), "why": p.why}
        for i, p in enumerate(fresh)
    ]
    base = {
        "plan_hash": plan_hash(pieces),
        "failures": previous.get("failures", 0),
        "filed": list(previous.get("filed", [])),
        "at": int(time.time() * 1000) if now_ms is None else now_ms,
    }
    try:
        output = runner.run(PURPOSE, {**state, "pieces": wire}, _params(fresh, start, not known), decider=decide)
    except ClassifierError:
        rows = known + [_row(piece, None, None) for piece in fresh]
        return _record(UNCHECKED, rows, previous.get("size") if known else None, [NO_ANSWER], "", False, base)
    result, thresholds = output.raw, output.thresholds
    rows = known + [
        _row(piece, result.answers[f"piece_{start + i}"].noul, thresholds["off_intent"])
        for i, piece in enumerate(fresh)
    ]
    if known:
        return _record(PASS, rows, previous.get("size"), [], result.source, result.calibrated, base)
    size = _size(result.answers["size"], levels(output.definition))
    reasons = failures(rows, size, thresholds["too_big_confidence"])
    verdict = FAIL if reasons else PASS
    base["failures"] += verdict == FAIL
    return _record(verdict, rows, size, reasons, result.source, result.calibrated, base)


def _record(verdict, rows, size, reasons, source, calibrated, base):
    return {
        "verdict": verdict,
        "plan_hash": base["plan_hash"],
        "pieces": rows,
        "size": size,
        "reasons": reasons,
        "source": source,
        "calibrated": calibrated,
        "failures": base["failures"],
        "filed": base["filed"],
        "at": base["at"],
    }


def load(folder):
    try:
        record = json.loads((folder / VERDICT).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def save(folder, record):
    path = folder / VERDICT
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    staged.write_text(json.dumps(record))
    os.replace(staged, path)


def _cuts(record):
    rows = [row for row in record["pieces"] if not row["kept"] and _row_key(row) not in record["filed"]]
    record["filed"] += [_row_key(row) for row in rows]
    return rows


def _file_cuts(rows, ledger, who, home):
    for row in rows:
        ledger.followup(who.swarm, followup_text(who.task, row["what"]))
        gate_log.append(who.swarm, gate_log.Row.of(GATE.name, "count", who, reason=f"cut: {row['what']}"), home)


def run(folder, state, ledger, who, mode, home=None, now_ms=None):
    """Trace the plan in folder; returns the verdict record and whether the task must be blocked."""
    try:
        text = (folder / PLAN).read_text()
    except OSError:
        raise ValueError(f"write the plan first: {folder / PLAN}, {FORMAT}") from None
    pieces = parse(text, who.task, state.get("task ids", ()))
    previous = load(folder)
    if previous.get("plan_hash") == plan_hash(pieces):
        return previous, False
    record, cuts = trace(pieces, state, previous, now_ms), []
    if record["verdict"] == PASS:
        cuts = _cuts(record)
    elif record["verdict"] == UNCHECKED:
        gate_log.append(who.swarm, gate_log.Row.of(GATE.name, "fail-open", who, reason=NO_ANSWER), home)
    elif mode in FAIL_KINDS:
        reason = " and ".join(record["reasons"])
        gate_log.append(who.swarm, gate_log.Row.of(GATE.name, FAIL_KINDS[mode], who, reason=reason), home)
    save(folder, record)
    _file_cuts(cuts, ledger, who, home)
    block = record["verdict"] == FAIL and record["failures"] > RETURNS and mode == "enforce"
    return record, block


def block_note(record):
    return f"Blocked by the plan trace after {record['failures']} failed plans: {' and '.join(record['reasons'])}"


def report(task_id, record, block):
    if block:
        step = "stop now; the plan failed twice and the task is blocked"
    elif record["verdict"] == FAIL:
        step = f"revise plan.md and run trace-plan again: {' and '.join(record['reasons'])}"
    elif record["verdict"] == UNCHECKED:
        step = f"{NO_ANSWER}; edits are allowed and counted, run trace-plan again later"
    else:
        step = "edit only inside the kept pieces' areas; each cut piece is a ledger follow up"
    return {
        "task": task_id,
        "verdict": record["verdict"],
        "kept": [row["what"] for row in record["pieces"] if row["kept"]],
        "cut": [row["what"] for row in record["pieces"] if not row["kept"]],
        "reasons": record["reasons"],
        "next": step,
    }


def __getattr__(name):
    if name == "SIZES":
        return levels(definitions.load(PURPOSE))
    raise AttributeError(name)
