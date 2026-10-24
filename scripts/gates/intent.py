"""The intent check: the tick asks the classifier whether the phase can use a task's pull request as delivered, and the
intent gate refuses merge and done while that answer is fail, or pending for under two minutes."""

import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable

from hooks.classifier import ClassifierError, code_rules, decide, definitions, runner
from hooks.classifier.questions import MAX_QUESTIONS
from scripts.gates import intent_history, lift, log
from scripts.gates.base import Decision, Who
from scripts.gates.identity import program_index, simple_commands
from scripts.gates.verdicts import Verdicts
from scripts.swarm import timing
from scripts.swarm_ledger import ledger_splits, plan_read

NAME = "intent"
PURPOSE = "intent-check"
MODES = ("enforce", "observe", "off", "coach")
DEFAULT_MODE = "observe"
PENDING, PASS, FAIL, UNCHECKED = "pending", "pass", "fail", "unchecked"
RUNNING = "intent check running"
GRACE_MS = 2 * 60_000
GH_TIMEOUT_SEC = 20
PROOF_CHARS = 4000
FAIL_COMMENT = "The intent check failed. The engineer has the verdict and the fix steps in the inbox."
SHORTFALL_COMMENT = "Intent remains unmet after two fix rounds. The master must review this shortfall in the gate log."
MERGED_COMMENT = "Intent remains unmet on a merged pull request. The master must review this shortfall in the gate log."
DRAFT = "draft pull request, judged once ready for review"
START, END = "<!-- agentihooks intent -->", "<!-- /agentihooks intent -->"
MERGE_QUEUE = ["merge", "queue"]
RELEASES = (["done"], ["ready"])
PULL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
REPO_FLAGS = ("-R", "--repo")
VALUE_FLAGS = ("-A", "--author-email", "-b", "--body", "-F", "--body-file", "--match-head-commit", "-t", "--subject")
SHORT = re.compile(r"(?<![\w./-])([A-Za-z0-9-]+)/([\w.-]+)#(\d+)\b")
NAMED_LIMIT = 3
LISTED = 4
NON_PRODUCTION = ("tests/", "docs/")
COMPARED = "[.status, [(.files // [])[].filename]]"
COMPARE_FILES = 300
DIFF_CHARS, BODY_CHARS = 6000, 1000
SECTION = re.compile(f"{re.escape(START)}.*?{re.escape(END)}", re.S)
TESTS_FIRST = f"{PURPOSE}-tests-first"
PLAN = f"{PURPOSE}-plan"
BASE_QUESTIONS = ("usable", "aligned", "delivers", "reachable", "weakens")
PLAN_QUESTIONS = ("aligned", "covers", "weakens")
DETAIL = ("stage", "criterion", "model", "definition", "definition_digest")
CHUNK_QUESTIONS = ("underdelivers", "overdelivers")
TESTS_FIRST_GUIDANCE = (
    "What would meet intent: Accept both old and new gate states in the preparatory tests "
    "without changing gate behaviour. Deliver the gate implementation in the later pull request."
)
REASONS = {
    "delivers": "the change may not deliver what the task text asks",
    "reachable": "nothing in the change may let the phase reach it",
}


def mode_of(config):
    chosen = config.gates.get(NAME)
    return chosen if chosen in MODES else DEFAULT_MODE


def _tests_first(body: str) -> bool:
    return "Task part: tests-first" in body.splitlines() or bool(
        re.search(
            r"(?:^|[.!?]\s+)(?:This tests[- ]first prerequisite\b|Tests[- ]first prerequisite for #\d+\.(?=\s|$))",
            body,
            re.MULTILINE,
        )
    )


def _pr_diff(url: str, run) -> str | None:
    try:
        patch = _gh(["gh", "pr", "diff", url], run)
        return patch.stdout if patch.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _phase(doc, task):
    return next((p for p in doc["phases"] if p.get("id") == task.get("phase")), {})


def _same_phase(record, task):
    return record.get("phase") == task.get("phase")


def section(doc, task):
    phase = _phase(doc, task)
    declared = _declared(task)
    delivers = f"declared {declared['kind']}: {declared['scope']}" if declared else task["description"]
    return (
        f"{START}\n## Parent intent\n\n**Project:** {doc['overview']}\n\n"
        f"**Phase {phase.get('title', '')}:** {phase.get('description', '')}\n\n"
        f"**Task {task['id']}, {task['title']}:** {delivers}\n{END}"
    )


def _declared(task):
    return (task.get("pull_scopes") or {}).get(task.get("pr_url"))


def with_intent(body, text):
    if SECTION.search(body):
        return SECTION.sub(lambda _: text, body, count=1)
    return f"{body.rstrip()}\n\n{text}" if body.strip() else text


def _gh(args, run, data=None):
    return run(args, input=data, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC)


def stamp_body(url, doc, task, run=subprocess.run):
    found = PULL.search(url)
    if not found:
        return False
    owner, repo, number = found.groups()
    try:
        read = _gh(["gh", "pr", "view", url, "--json", "body"], run)
        if read.returncode:
            return False
        body = with_intent(json.loads(read.stdout).get("body") or "", section(doc, {**task, "pr_url": url}))
        path = f"repos/{owner}/{repo}/pulls/{number}"
        sent = _gh(["gh", "api", "--method", "PATCH", path, "--input", "-"], run, json.dumps({"body": body}))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return sent.returncode == 0


def stamp(slug, task_id, url, doc, mode, now_ms, home=None):
    task = next((t for t in doc["tasks"] if t.get("id") == task_id), None)
    body = task is not None and stamp_body(url, doc, task)
    if mode == "off":
        return {"verdict": "off", "body": body}
    verdicts = Verdicts(slug, NAME, home)
    planned = verdicts.read(task_id)
    _bind_pulls(verdicts, task, url, doc)
    if task is not None and not task.get("split") and planned and planned.get("planned") and planned["verdict"] == PASS:
        if _same_phase(planned, task):
            return {"verdict": PASS, "body": body}
    verdicts.write(task_id, PENDING, RUNNING, now_ms, **_split_fields(task, url))
    return {"verdict": PENDING, "body": body}


def _split_fields(task, url):
    return {"url": url, "split": True} if task and task.get("split") else {}


def _pull_subject(task, url):
    return f"{task}.{hashlib.sha256(url.encode()).hexdigest()}"


def _bind_pulls(state, task, url, doc):
    if task and task.get("split"):
        from scripts.swarm.done_gate import _pulls

        task = {**task, "pr_url": url}
        pulls = [pull for pull in _pulls(task) if PULL.search(pull)]
        inputs = {pull: _fingerprint(doc, {**task, "pr_url": pull}) for pull in pulls}
        Verdicts(state.slug, f"{NAME}-pulls", state.home).write(
            task["id"],
            PASS,
            "registered split pulls",
            pulls=pulls,
            inputs=inputs,
        )


def _pr_field(url, field, run):
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    try:
        result = _gh(["gh", "api", f"repos/{owner}/{repo}/pulls/{number}", "--jq", f".{field}"], run)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def pr_merged(url: str, run=subprocess.run) -> bool:
    return _pr_field(url, "merged", run) == "true"


def pr_head(url: str, run=subprocess.run) -> str | None:
    return _pr_field(url, "head.sha", run)


def production_unchanged(url: str, before: str, after: str, run=subprocess.run) -> bool:
    found = PULL.search(url)
    if not found:
        return False
    owner, repo, _ = found.groups()
    compare = ["gh", "api", f"repos/{owner}/{repo}/compare/{before}...{after}", "--jq", COMPARED]
    try:
        result = _gh(compare, run)
        status, files = json.loads(result.stdout) if result.returncode == 0 else (None, [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return status == "ahead" and len(files) < COMPARE_FILES and all(path.startswith(NON_PRODUCTION) for path in files)


def text_key(title: str, body: str | None) -> str:
    return hashlib.sha256(json.dumps([title, body or ""]).encode()).hexdigest()


def pr_text(url: str, run=subprocess.run) -> str | None:
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    try:
        result = _gh(["gh", "api", f"repos/{owner}/{repo}/pulls/{number}", "--jq", "{title, body}"], run)
        raw = json.loads(result.stdout) if result.returncode == 0 else None
        return text_key(raw["title"], raw["body"]) if raw else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def pr_view(url, run=subprocess.run):
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    before = pr_head(url, run)
    if not before:
        return None
    try:
        done = _gh(["gh", "pr", "view", url, "--json", "title,body,files,reviews,comments,isDraft,state"], run)
        raw = json.loads(done.stdout) if done.returncode == 0 else None
        if raw is None:
            return None
        title, body, files = raw["title"], raw["body"] or "", [f["path"] for f in raw["files"]]
        path = f"repos/{owner}/{repo}/pulls/{number}/comments"
        comments = _gh(["gh", "api", "--paginate", "--jq", ".[] | @json", path], run)
        if comments.returncode:
            return None
        diff = {}
        if _tests_first(body):
            diff = {"diff": _pr_diff(url, run)}
        head = pr_head(url, run)
        if not head or head != before:
            return None
        return {
            "head": head,
            "title": title,
            "body": body,
            "files": files,
            "draft": bool(raw.get("isDraft")),
            "merged": raw.get("state") == "MERGED",
            **diff,
            "reviewer_findings": {
                "reviews": raw.get("reviews", []),
                "comments": raw.get("comments", []),
                "inline": [json.loads(line) for line in comments.stdout.splitlines() if line.strip()],
            },
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def named_pulls(task, body):
    own = PULL.search(task["pr_url"])
    here = (own[1].lower(), own[2].lower()) if own else None
    texts = [task.get("description"), SECTION.sub("", body), *(c.get("text") for c in task.get("comments") or [])]
    urls, seen = [], set()
    for text in filter(None, texts):
        short = (m.groups() for m in SHORT.finditer(text) if here and m[1].lower() == here[0])
        for owner, repo, number in [*(m.groups() for m in PULL.finditer(text)), *short]:
            url = f"https://github.com/{owner}/{repo}/pull/{number}"
            if (owner.lower(), repo.lower()) != here and url.lower() not in seen:
                seen.add(url.lower())
                urls.append(url)
    return urls


def named_view(url, run=subprocess.run):
    try:
        done = _gh(["gh", "pr", "view", url, "--json", "title,body,files,state"], run)
        raw = json.loads(done.stdout) if done.returncode == 0 else None
        if raw is None:
            return None
        body, diff = raw["body"] or "", _pr_diff(url, run)
        return {
            "url": url,
            "title": raw["title"],
            "body": body[:BODY_CHARS],
            "body_truncated": len(body) > BODY_CHARS,
            "files": [f["path"] for f in raw["files"]],
            "merged": raw.get("state") == "MERGED",
            "diff": None if diff is None else diff[:DIFF_CHARS],
            "diff_truncated": diff is not None and len(diff) > DIFF_CHARS,
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def _proof_notes(task, proof_chars):
    try:
        return (Path(task["workspace"]) / "proof.md").read_text()[-proof_chars:]
    except (KeyError, OSError):
        return ""


def _split_half(task):
    halves, pulls = task.get("split") or [], task.get("split_pulls") or []
    index = pulls.index(task.get("pr_url")) if task.get("pr_url") in pulls else len(pulls)
    return (index, halves[index]) if index < len(halves) else None


def _split_note(task):
    half = _split_half(task)
    if half is None:
        return ""
    index, lines = half
    ranges = task["split"]
    kind = "plan lines " if ledger_splits.ranged(ranges) else ""
    others = "; ".join(f"half {n} carries {kind}{other}" for n, other in enumerate(ranges, 1) if n != index + 1)
    return (
        f"\n\nThis pull request is half {index + 1} of {len(ranges)} of a split task: it carries only {kind}"
        f"{lines}; {others} in its own pull request."
    )


def _plan_chunk(doc, task):
    half = _split_half(task) if task.get("split") and ledger_splits.ranged(task["split"]) else None
    task = {**task, "plan_lines": half[1]} if half else task
    if not task.get("plan_lines") or task.get("follow_up") or _declared(task):
        return {}
    try:
        text = plan_read.exact(doc, task)
    except (ValueError, OSError):
        text = None
    part = {"plan_lines": task["plan_lines"], "plan_chunk": text}
    if task.get("plan_slice") != task.get("id"):
        part = plan_read.scoped(doc, task, part)
    if "shared_slice" not in part and isinstance(text, str) and _foreign_slice(doc, task, text):
        return {}
    return part


def _foreign_slice(doc, task, text):
    name = task.get("plan_slice")
    if not name:
        return False
    named = re.escape(name)
    marked = rf"^\s*(?:<!--\s*slice:\s*{named}\s*-->|#+ .*(?<![\w.-]){named}(?![\w.-]).*)$"
    if not re.search(marked, text, re.M):
        return True
    if name == task.get("id"):
        return False
    return any(
        other.get("id") == name or (other.get("plan_slice") == name and other.get("phase") == task.get("phase"))
        for other in doc.get("tasks", ())
        if other.get("id") != task.get("id")
    )


def _parent(doc, task):
    phase = _phase(doc, task)
    return {
        "overview": doc["overview"],
        "phase": phase.get("title", ""),
        "phase_intent": phase.get("description", ""),
        "task": task["title"],
        "task_text": _task_text(task),
    }


def _task_text(task):
    declared = _declared(task)
    if declared is None:
        return task["description"] + _split_note(task)
    return (
        f"This pull request is a declared {declared['kind']} of this task and delivers only: {declared['scope']}. "
        "The whole task is judged at its final pull request."
    )


def _task_contract(doc, task):
    phase = _phase(doc, task)
    if task.get("split") and not ledger_splits.ranged(task["split"]):
        return {**task, "plan_lines": ""}
    if task.get("split") and (
        task.get("follow_up") or not (task.get("plan_url") or phase.get("plan_url") or phase.get("plan_ref"))
    ):
        return {**task, "split": [], "plan_lines": ""}
    return task


def state_of(doc, task, pr, proof_chars=PROOF_CHARS):
    task = _task_contract(doc, task)
    return {
        **_parent(doc, task),
        "pull_request_title": pr["title"],
        "pull_request_body": pr["body"],
        "changed_files": pr["files"],
        "proof": task.get("proof") or {},
        "proof_notes": _proof_notes(task, proof_chars),
        "reviewer_findings": pr.get("reviewer_findings", {}),
        **({"task_part": "tests-first", "pull_request_diff": pr.get("diff")} if _tests_first(pr["body"]) else {}),
        **_plan_chunk(doc, task),
        **_named_state(pr),
    }


def plan_state(doc, task, kept):
    task = _task_contract(doc, task)
    return {
        "stage": "plan",
        **_parent(doc, task),
        "planned_pieces": [f"{row['what']} | {', '.join(row['areas'])} | {row['why']}" for row in kept],
        "planned_areas": sorted({area for row in kept for area in row["areas"]}),
        **_plan_chunk(doc, task),
    }


def _named_state(pr):
    if "pull_requests" not in pr:
        return {}
    pulls = pr["pull_requests"]
    return {
        "pull_requests": pulls,
        "judged_pull_requests": [p["url"] for p in pulls],
        "unread_pull_requests": pr["unread_pull_requests"],
        **({"unread_overflow": pr["unread_overflow"]} if "unread_overflow" in pr else {}),
    }


def _listed(urls, more):
    more += len(urls[LISTED:])
    if more:
        return f"{', '.join(urls[:LISTED])} and {more} more"
    return f"{', '.join(urls[:-1])} and {urls[-1]}" if len(urls) > 1 else urls[0]


def _judged_on(state):
    judged, unread = state.get("judged_pull_requests") or [], state.get("unread_pull_requests") or []
    text = f"; judged on {_listed(judged, 0)}" if judged else ""
    return text + (f"; could not read {_listed(unread, state.get('unread_overflow', 0))}" if unread else "")


def _pending(pr):
    return {"named_pending": pr["named_pending"]} if pr.get("named_pending") else {}


def _rows(state):
    if state.get("task_part") == "tests-first":
        return []
    text = state.get("plan_chunk")
    return plan_read.numbered(text, state["plan_lines"]) if isinstance(text, str) else []


def _stage(state):
    if state.get("task_part") == "tests-first":
        return "tests-first"
    return "plan" if state.get("stage") == "plan" else "delivered"


def _definition(state):
    return {"tests-first": TESTS_FIRST, "plan": PLAN}.get(_stage(state), PURPOSE)


def _questions(definition, state, params):
    if state.get("task_part") == "tests-first":
        return runner.questions_for(definition)
    base = PLAN_QUESTIONS if definition.name == PLAN else BASE_QUESTIONS
    rows = _rows(state)
    fits = len(base) + len(CHUNK_QUESTIONS) + len(rows) <= MAX_QUESTIONS
    asked = runner.questions_for(definition, {**params, "rows": [list(row) for row in rows] if fits else []})
    if not rows:
        return {key: asked[key] for key in base}
    lines = {f"misses_line_{number}": asked[f"misses_line_{index}"] for index, (number, _) in enumerate(rows) if fits}
    return {**{key: asked[key] for key in (*base, *CHUNK_QUESTIONS)}, **lines}


def questions_for(state):
    return _questions(definitions.load(_definition(state)), state, {})


def _quoted(rows):
    return ", ".join(f'line {number} "{row}"' for number, row in rows)


def _chunk_reasons(state, answers, thresholds, subject="the change"):
    if not _rows(state):
        return []
    lines, under, over = state["plan_lines"], answers["underdelivers"].noul, answers["overdelivers"].noul
    reasons = []
    if under >= thresholds["chunk"]:
        reasons.append(f"{subject} may leave out something plan lines {lines} ask for, at probability {under:.2f}")
    if over >= thresholds["chunk"]:
        reasons.append(f"{subject} may add scope plan lines {lines} do not ask for, at probability {over:.2f}")
    return reasons


def _chunk_steps(state, answers, thresholds, subject="the change"):
    rows, lines, steps = _rows(state), state.get("plan_lines"), []
    if not rows:
        return steps
    line = thresholds["chunk"]
    if answers["underdelivers"].noul >= line:
        named = [(n, row) for n, row in rows if f"misses_line_{n}" in answers]
        missed = [(n, row) for n, row in named if answers[f"misses_line_{n}"].noul >= line]
        if missed:
            steps.append(f"Deliver what plan lines {lines} ask for and {subject} leaves out: {_quoted(missed)}.")
        else:
            steps.append(
                f"Deliver what plan lines {lines} ask for. No single line was named, so check each: {_quoted(rows)}."
            )
    if answers["overdelivers"].noul >= line:
        steps.append(f"Remove the scope beyond plan lines {lines}, which ask only for {_quoted(rows)}.")
    return steps


def remediation(state: dict, answers: dict, thresholds: dict) -> str:
    if state.get("task_part") == "tests-first":
        return TESTS_FIRST_GUIDANCE
    steps = []
    if state.get("task_text"):
        task = f"{state['task']}: {state['task_text']}"
        phase = f"{state['phase']}: {state['phase_intent']}"
        steps.append(f"Deliver {task}. The phase must be able to use it for {phase}.")
        if answers["delivers"].noul < thresholds["reason"]:
            steps.append(f"Implement the missing acceptance behavior described by {task}.")
        if answers["reachable"].noul < thresholds["reason"]:
            steps.append(
                f"Wire the production entrypoint for {state['task']} and prove an invocation delivers {phase}."
            )
        if answers["weakens"].noul >= thresholds["weaken"]:
            steps.append(f"Preserve {phase} while implementing {task}.")
    steps += _chunk_steps(state, answers, thresholds)
    return "What would meet intent: " + " ".join(steps) if steps else ""


def _verdict(state, answers, thresholds):
    if state.get("task_part") == "tests-first":
        faults = []
        if answers["accepts_both_states"].noul < thresholds["reason"]:
            faults.append("the preparatory tests may not accept both old and new gate states")
        if answers["changes_gate_behavior"].noul >= thresholds["weaken"]:
            faults.append("the tests first part may change gate behaviour")
        if faults:
            return FAIL, "; ".join([*faults, TESTS_FIRST_GUIDANCE]), "tests-first"
    if state.get("stage") == "plan":
        return _plan_verdict(state, answers, thresholds)
    usable, weakens, fail = answers["usable"].noul, answers["weakens"].noul, thresholds["fail"]
    chunk = _chunk_reasons(state, answers, thresholds)
    if usable >= fail and weakens < thresholds["weaken"] and not chunk:
        cited = f", judged against plan lines {state['plan_lines']}" if _rows(state) else ""
        return PASS, f"the phase can use it as delivered at probability {usable:.2f}{cited}", None
    lead, criterion = _delivered_lead(answers, thresholds)
    reasons = [text for key, text in REASONS.items() if answers[key].noul < thresholds["reason"]]
    if weakens >= thresholds["weaken"]:
        reasons.append(f"the change may weaken what the phase builds, at probability {weakens:.2f}")
    reasons += chunk
    guidance = remediation(state, answers, thresholds)
    if guidance:
        reasons.append(guidance)
    return FAIL, "; ".join([lead, *reasons]), criterion


def _delivered_lead(answers, thresholds):
    usable, fail = answers["usable"].noul, thresholds["fail"]
    if usable >= fail:
        weakened = answers["weakens"].noul >= thresholds["weaken"]
        return f"the phase can use this change at probability {usable:.2f}", "weakens" if weakened else "chunk"
    if "aligned" not in answers:
        return f"the phase can use this change at probability {usable:.2f}, under {fail}", "usable"
    aligned = answers["aligned"].noul
    if aligned >= fail:
        return (
            f"the change fits the phase at probability {aligned:.2f}, and the phase can use it as delivered at "
            f"probability {usable:.2f}, under {fail}: a delivery shortfall, not a phase mismatch",
            "delivery",
        )
    return (
        f"the change may not fit the phase: it fits at probability {aligned:.2f}, under {fail}, and the phase can use "
        f"it at probability {usable:.2f}",
        "phase",
    )


def _plan_verdict(state, answers, thresholds):
    aligned, covers, weakens = (answers[key].noul for key in PLAN_QUESTIONS)
    fail = thresholds["fail"]
    faults = []
    if aligned < fail:
        faults.append(
            ("phase", f"the plan may not serve the phase: it serves it at probability {aligned:.2f}, under {fail}")
        )
    if covers < fail:
        faults.append(
            (
                "coverage",
                f"the plan may leave out what the task asks: it covers it at probability {covers:.2f}, under {fail}",
            )
        )
    if weakens >= thresholds["weaken"]:
        faults.append(("weakens", f"the planned work may weaken what the phase builds, at probability {weakens:.2f}"))
    faults += [("chunk", text) for text in _chunk_reasons(state, answers, thresholds, "the plan")]
    if not faults:
        cited = f", judged against plan lines {state['plan_lines']}" if _rows(state) else ""
        served = f"the plan serves the phase at probability {aligned:.2f}"
        return PASS, f"{served} and covers the task at probability {covers:.2f}{cited}", None
    task, phase = f"{state['task']}: {state['task_text']}", f"{state['phase']}: {state['phase_intent']}"
    steps = [
        f"Plan work that serves {phase}, and covers {task}.",
        *_chunk_steps(state, answers, thresholds, "the plan"),
    ]
    return FAIL, "; ".join([*(text for _, text in faults), "What would meet intent: " + " ".join(steps)]), faults[0][0]


def _verdicts(definition, state, params, answers):
    thresholds = definition.thresholds if definition.name == PURPOSE else definitions.load(PURPOSE).thresholds
    verdict, reason, criterion = _verdict(state, answers, thresholds)
    shared = f"; {state['shared_slice']}" if state.get("shared_slice") else ""
    return {"verdict": verdict, "reason": reason + _judged_on(state) + shared, "criterion": criterion}


RULE = code_rules.CodeRule(_questions, _verdicts, {"verdict": (PASS, FAIL, UNCHECKED)}, {"verdict": FAIL})


def judged(state, decide=decide):
    stage = _stage(state)
    if stage == "tests-first" and not isinstance(state.get("pull_request_diff"), str):
        reason = "the tests first part requires a complete pull request diff to judge gate behaviour"
        return {"verdict": FAIL, "reason": reason, "stage": stage, "criterion": "tests-first"}
    if stage != "tests-first" and state.get("plan_lines") and not isinstance(state.get("plan_chunk"), str):
        reason = f"the plan chunk for lines {state['plan_lines']} could not be read"
        return {"verdict": UNCHECKED, "reason": reason, "stage": stage}
    try:
        ran = runner.run(_definition(state), state, decider=decide)
    except definitions.DefinitionError as exc:
        return {"verdict": FAIL, "reason": f"the intent definition is invalid: {exc}", "stage": stage}
    except ClassifierError:
        return {"verdict": UNCHECKED, "reason": "the classifier did not answer", "stage": stage}
    return {
        **ran.verdicts,
        "stage": stage,
        "model": getattr(ran.raw, "source", None),
        "definition": ran.definition.name,
        "definition_digest": ran.definition.digest,
    }


def judge(state, decide=decide):
    answer = judged(state, decide)
    return answer["verdict"], answer["reason"]


def fix_steps(slug: str) -> str:
    return (
        "Fix steps: Deliver the missing parent intent behavior identified in the verdict, "
        "add evidence that the phase can use it as delivered, commit and push the fix, "
        f"then run agentihooks swarm {slug} pr <url> for a new check."
    )


def _fail_kind(mode):
    return "deny" if mode == "enforce" else "observe"


def _answer(value):
    return value if isinstance(value, dict) else {"verdict": value[0], "reason": value[1]}


def _detail(answer):
    return {key: answer[key] for key in DETAIL if key in answer}


def _remember(slug, task, now_ms, state, answer, home):
    record = {
        "task": task["id"],
        "agent": task.get("claimed_by", ""),
        "at": now_ms,
        "purpose": PURPOSE,
        "verdict": answer["verdict"],
        "reason": answer["reason"],
        **_detail(answer),
        "classifier_input": intent_history.request(state, questions_for(state)),
    }
    intent_history.append(slug, record, home)


def plan_check(slug, doc, task_id, traced, mode, now_ms, home=None):
    task = next((t for t in doc["tasks"] if t.get("id") == task_id), None)
    if mode == "off" or task is None or task.get("pr_url"):
        return None
    verdicts = Verdicts(slug, NAME, home)
    previous = verdicts.read(task_id)
    if previous and previous.get("planned") == traced["plan_hash"] and _same_phase(previous, task):
        return {"verdict": previous["verdict"], "reason": previous["reason"]}
    kept = [row for row in traced["pieces"] if row["kept"]]
    state = intent_history.prepare(plan_state(doc, task, kept))
    answer = judged(state)
    verdict, reason = answer["verdict"], answer["reason"]
    _remember(slug, task, now_ms, state, answer, home)
    fields = {"phase": task.get("phase"), "planned": traced["plan_hash"], **_detail(answer)}
    verdicts.write(task_id, verdict, reason, now_ms, **fields)
    who = Who(name=task.get("claimed_by", ""), task=task_id)
    if verdict == UNCHECKED:
        log.append(slug, log.Row.of(NAME, "count", who, reason=reason), home)
    elif verdict == FAIL:
        log.append(slug, log.Row.of(NAME, _fail_kind(mode), who, reason=reason), home)
    return {"verdict": verdict, "reason": reason}


def _fingerprint(doc, task):
    phase = _phase(doc, task)
    parent = [task.get("title"), task.get("description"), phase.get("title"), phase.get("description")]
    declared = _declared(task)
    inputs = [*parent, _plan_chunk(doc, task), *([declared] if declared else [])]
    return hashlib.sha256(json.dumps(inputs).encode()).hexdigest()


def _drafted(record):
    return bool(record) and record["verdict"] == PENDING and record["reason"] == DRAFT


@dataclass(frozen=True)
class _Judgment:
    pr: dict | None
    previous: dict | None
    state: dict | None
    answer: dict | tuple[str, str] | None
    inputs: str | None = None


@dataclass(frozen=True)
class Check:
    slug: str
    mode: str
    now_ms: int
    ledger: object
    mail: object
    view: Callable
    ask: Callable
    home: object = None
    head: Callable | None = None
    named: Callable | None = None
    text: Callable | None = None

    def run(self, doc):
        if self.mode == "off":
            return []
        verdicts, actions, tasks = Verdicts(self.slug, NAME, self.home), [], []
        for task in doc["tasks"]:
            states = ("pr", "claimed") if self.mode == "coach" else ("pr",)
            if task.get("state") not in states or not task.get("pr_url"):
                continue
            actions += self._isolated(task, self._queue, doc, task, verdicts, tasks)
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [
                (task, record, moved, workers.submit(copy_context().run, self._judge, doc, task))
                for task, record, moved in tasks
            ]
            for task, record, moved, future in pending:
                actions += self._isolated(task, self._settle, task, record, moved, future, verdicts)
        return actions

    def _queue(self, doc, task, verdicts, tasks):
        _bind_pulls(verdicts, task, task["pr_url"], doc)
        record = verdicts.read(task["id"])
        judged = record and record["verdict"] != PENDING and not record.get("planned")
        moved = (
            self.mode != "coach"
            and judged
            and (record.get("inputs") != _fingerprint(doc, task) or self._named_moved(record))
        )
        if self.mode != "coach" and judged and _same_phase(record, task) and not moved:
            return []
        if _drafted(record):
            verdicts.write(
                task["id"],
                PENDING,
                RUNNING,
                self.now_ms,
                phase=task.get("phase"),
                **_split_fields(task, task["pr_url"]),
            )
        tasks.append((task, record, moved))
        return []

    def _isolated(self, task, step, *args):
        from scripts.swarm.ledger_client import LedgerRefused

        try:
            return step(*args)
        except LedgerRefused:
            raise
        except Exception as exc:  # one task's failure is logged and never ends the tick run
            who = Who(name=task.get("claimed_by", ""), task=task["id"])
            log.append(self.slug, log.Row.of(NAME, "fail-open", who, reason=f"{type(exc).__name__}: {exc}"), self.home)
            return [f"task {task['id']} intent check failed: {type(exc).__name__}"]

    def _settle(self, task, record, moved, future, verdicts):
        if not record or not _same_phase(record, task) or moved:
            verdicts.write(
                task["id"],
                PENDING,
                RUNNING,
                self.now_ms,
                phase=task.get("phase"),
                **_split_fields(task, task["pr_url"]),
            )
        judgment = future.result()
        if judgment is None and _drafted(record):
            verdicts.write(
                task["id"],
                PENDING,
                DRAFT,
                record["at"],
                phase=record.get("phase"),
                **_split_fields(task, task["pr_url"]),
            )
        if judgment is None:
            return []
        actions = self._check(task, judgment, verdicts)
        timing.keep()
        return actions

    def _judge(self, doc, task):
        inputs = _fingerprint(doc, task)
        previous = self._unmoved(task, inputs)
        if previous:
            return _Judgment(None, previous, None, None)
        pr = self.view(task["pr_url"])
        if pr is None or (self.mode == "coach" and not pr.get("head")):
            return None
        if pr.get("draft"):
            return _Judgment(pr, None, None, None)
        previous = self._coaching().read(task["id"]) if self.mode == "coach" else None
        previous = previous if previous and _same_phase(previous, task) else None
        if (
            previous
            and previous["head"] == pr.get("head")
            and previous.get("inputs") == inputs
            and previous.get("text") == text_key(pr["title"], pr["body"])
            and not self._named_moved(previous)
        ):
            return _Judgment(pr, previous, None, None)
        if previous and previous.get("inputs") == inputs and lift.head_lifted(self.slug, task["id"], NAME, self.home):
            return _Judgment(pr, previous, None, None)
        pr = self._with_named(task, pr)
        state = intent_history.prepare(state_of(doc, task, pr))
        if state.get("task_part") == "tests-first":
            state["pull_request_diff"] = intent_history.masked(pr.get("diff"))
        return _Judgment(pr, previous, state, self.ask(state), inputs)

    def _with_named(self, task, pr):
        others = named_pulls(task, pr["body"]) if self.named else []
        if not others:
            return pr
        views = {url: self.named(url) for url in [task["pr_url"], *others[:NAMED_LIMIT]]}
        pulls = [view for view in views.values() if view]
        failed = [url for url, view in views.items() if not view]
        opened = [p["url"] for p in pulls if p["url"] != task["pr_url"] and not p["merged"]]
        unread = failed + others[NAMED_LIMIT:]
        overflow = len(unread[intent_history.URL_COUNT :])
        return {
            **pr,
            "pull_requests": pulls,
            "unread_pull_requests": unread[: intent_history.URL_COUNT],
            **({"unread_overflow": overflow} if overflow else {}),
            "named_pending": failed + opened,
        }

    def _named_moved(self, record):
        pending = record.get("named_pending") or []
        return bool(self.named) and any((view := self.named(url)) and view["merged"] for url in pending)

    def _check(self, task, judgment, verdicts):
        previous, pr = judgment.previous, judgment.pr
        if judgment.state is None and previous is None:
            verdicts.write(
                task["id"], PENDING, DRAFT, self.now_ms, phase=task.get("phase"), **_split_fields(task, task["pr_url"])
            )
            return []
        if judgment.state is None:
            self._keep(task, previous, verdicts)
            return []
        state, answer = judgment.state, _answer(judgment.answer)
        verdict, reason = answer["verdict"], answer["reason"]
        head = pr.get("head")
        fixed = bool(previous) and previous["verdict"] == FAIL and previous["head"] != head
        rounds = min(previous["coach_rounds"] + fixed, 2) if previous else 0
        _remember(self.slug, task, self.now_ms, state, answer, self.home)
        coached = (
            {"coach_rounds": rounds, "head": head, "url": task["pr_url"], "text": text_key(pr["title"], pr["body"])}
            if self.mode == "coach"
            else {}
        )
        fields = {
            **_detail(answer),
            "phase": task.get("phase"),
            **coached,
            "inputs": judgment.inputs,
            **_pending(pr),
            **_split_fields(task, task["pr_url"]),
            "persisted_at": int(time.time() * 1000),
        }
        record = verdicts.write(task["id"], verdict, reason, self.now_ms, **fields)
        self._save_pull(task, {**record, "head": head, "url": task["pr_url"]})
        if self.mode == "coach":
            self._coaching().write(task["id"], verdict, reason, self.now_ms, **fields)
        who = Who(name=task.get("claimed_by", ""), task=task["id"])
        actions = [f"task {task['id']} intent check {verdict}"]
        if verdict == UNCHECKED:
            log.append(self.slug, log.Row.of(NAME, "count", who, reason=reason), self.home)
        elif verdict == FAIL:
            actions += self._failed(task, who, reason, rounds, pr.get("merged"))
        elif self.mode == "coach" and task.get("state") == "claimed":
            self.ledger.update_task(self.slug, task["id"], {"state": "pr"})
        return actions

    def _coaching(self):
        return Verdicts(self.slug, "intent-coach", self.home)

    def _unmoved(self, task, inputs):
        if self.mode != "coach" or self.head is None:
            return None
        previous = self._coaching().read(task["id"])
        if (
            not previous
            or not previous.get("head")
            or not _same_phase(previous, task)
            or previous.get("inputs") != inputs
            or previous["head"] != self.head(task["pr_url"])
            or self._text_moved(previous, task["pr_url"])
            or self._named_moved(previous)
        ):
            return None
        return previous

    def _text_moved(self, previous, url):
        if self.text is None:
            return False
        current = self.text(url)
        return current is None or previous.get("text") != current

    def _keep(self, task, previous, verdicts):
        verdicts.write(
            task["id"],
            previous["verdict"],
            previous["reason"],
            previous["at"],
            coach_rounds=previous["coach_rounds"],
            head=previous["head"],
            phase=task.get("phase"),
            persisted_at=previous.get("persisted_at"),
            **{"url": task["pr_url"], **_split_fields(task, task["pr_url"])},
        )

        self._save_pull(task, {**previous, "url": task["pr_url"]})

    def _save_pull(self, task, record):
        if task.get("split"):
            fields = {key: value for key, value in record.items() if key not in ("verdict", "reason", "at")}
            Verdicts(self.slug, f"{NAME}-pulls", self.home).write(
                _pull_subject(task["id"], task["pr_url"]),
                record["verdict"],
                record["reason"],
                record["at"],
                **fields,
            )

    def _failed(self, task, who, reason, rounds=0, merged=False):
        log.append(self.slug, log.Row.of(NAME, _fail_kind(self.mode), who, reason=reason), self.home)
        if self.mode == "coach" and (rounds >= 2 or merged):
            unmet = "on a merged pull request" if merged else "after two fix rounds"
            text = f"Intent remains unmet {unmet}: {reason}. The master must review this shortfall."
            key, ref = f"intent-shortfall:{task['id']}:{self.now_ms}", f"tasks/{task['id']}"
            told = self.mail.send(key, self.mail.master, text, ref=ref)
            if task.get("state") == "claimed":
                self.ledger.update_task(self.slug, task["id"], {"state": "pr"})
            comment = MERGED_COMMENT if merged else SHORTFALL_COMMENT
            self.ledger.comment(self.slug, task["id"], comment, by="swarm")
            return told
        if self.mode not in ("enforce", "coach"):
            return []
        text = f"The intent check failed: {reason}. {fix_steps(self.slug)}"
        if self.mode == "coach":
            text += f" Run fix round {rounds + 1} of 2; after two unsuccessful fix rounds merge with the shortfall recorded."
        key, ref = f"intent-fail:{task['id']}:{self.now_ms}", f"tasks/{task['id']}"
        told = self.mail.send(key, self.mail.engineer(task), text, ref=ref)
        self.ledger.update_task(self.slug, task["id"], {"state": "claimed"})
        self.ledger.comment(self.slug, task["id"], FAIL_COMMENT, by="swarm")
        return told


def _gated(words):
    index = program_index(words)
    if index is None:
        return False
    program, rest = PurePosixPath(words[index]).name, words[index + 1 :]
    if program == "gh":
        return rest[:2] == ["pr", "merge"]
    return program == "agentihooks" and rest[:1] == ["swarm"] and (rest[2:3] in RELEASES or rest[2:4] == MERGE_QUEUE)


def _merge_target(words):
    index = program_index(words)
    if index is None:
        return None
    program, rest = PurePosixPath(words[index]).name, words[index + 1 :]
    if program == "gh" and rest[:2] == ["pr", "merge"]:
        operands = rest[2:]
    elif program == "agentihooks" and rest[:1] == ["swarm"] and rest[2:4] == MERGE_QUEUE:
        operands = rest[4:]
    else:
        return None
    return next((word for word in operands if PULL.search(word) or word.isdigit()), None)


def _unnamed_merge(words):
    index = program_index(words)
    return (
        index is not None
        and PurePosixPath(words[index]).name == "gh"
        and words[index + 1 : index + 3] == ["pr", "merge"]
        and _merge_target(words) is None
    )


def _pull_view(words):
    rest, branch, repo = iter(words[program_index(words) + 3 :]), [], []
    for word in rest:
        if word in REPO_FLAGS:
            repo = [word, next(rest, "")]
        elif word.startswith(("--repo=", "-R")):
            repo = [word]
        elif word in VALUE_FLAGS:
            next(rest, None)
        elif not word.startswith("-") and not branch:
            branch = [word]
    return ["gh", "pr", "view", *branch, *repo, "--json", "url", "--jq", ".url"]


def _same_pull(target, url):
    named, bound = PULL.search(target), PULL.search(url)
    return bool(bound) and (named.groups() if named else target) == (bound.groups() if named else bound[3])


def _queue_target(command):
    for words in simple_commands(command):
        index = program_index(words)
        if PurePosixPath(words[index]).name == "agentihooks":
            return _merge_target(words)
    return None


def queue_decision(who: Who, state: Verdicts, url: str, mode: str, head: str | None = None) -> Decision:
    if mode == "off":
        return Decision()
    record = state.read(who.task)
    pulls = Verdicts(state.slug, f"{NAME}-pulls", state.home)
    bound = pulls.read(who.task)
    if bound:
        urls = [pull for pull in bound["pulls"] if _same_pull(url, pull)]
        if len(urls) != 1:
            return Decision.deny("Intent requires a pull request registered on this task.")
        url = urls[0]
        record = pulls.read(_pull_subject(who.task, url))
        if record and record.get("inputs") != bound["inputs"].get(url):
            return Decision()
    if not record or record["verdict"] != FAIL:
        return Decision()
    judged_url = record.get("url")
    if not judged_url or not _same_pull(url, judged_url) or not record.get("head"):
        return Decision()
    if (head if head is not None else pr_head(judged_url)) != record["head"]:
        return Decision()
    text = f"intent check failed for task {who.task}: {record['reason']}. {fix_steps(who.swarm)}"
    if mode != "enforce":
        log.append(state.slug, log.Row.of(NAME, mode, who, "Bash", text), state.home)
    if mode == "coach":
        print(text, file=sys.stderr)
    return Decision(allowed=mode != "enforce", reason=text if mode != "observe" else "")


def _other_half(command, record):
    judged = record and record.get("split") and PULL.search(record["url"])
    if not judged:
        return False
    for target in filter(None, map(_merge_target, simple_commands(command))):
        named = PULL.search(target)
        if (named.groups() if named else target) != (judged.groups() if named else judged[3]):
            return True
    return False


def _next_step(who):
    return (
        "Mark the pull request ready with gh pr ready <pull request url> if it is a draft, then wait for the verdict: "
        f"agentihooks swarm {who.swarm} wait --on intent <pull request url>"
    )


class IntentGate:
    name = NAME
    default_mode = DEFAULT_MODE

    def __init__(self, clock=time.time, run=subprocess.run):
        self.clock, self.run = clock, run

    def matches(self, call):
        return call.tool == "Bash" and any(word in call.command for word in ("merge", "done", "ready"))

    def decide(self, call, who, state, mode="enforce"):
        if not (who.pinned and who.task) or not any(map(_gated, simple_commands(call.command))):
            return Decision()
        record = state.read(who.task)
        pulls = Verdicts(state.slug, f"{NAME}-pulls", state.home)
        bound = pulls.read(who.task)
        call = self._resolve(call, bound or (record and record.get("split")))
        if call is None:
            return Decision.deny(
                f"task {who.task} is split: name the pull request url in the merge so intent matches it to its "
                "judged half."
            )
        if not bound:
            return self._decide_unbound(call, who, state, mode, record)
        commands = [words for words in simple_commands(call.command) if _gated(words)]
        for words in commands:
            single = replace(call, tool_input={**call.tool_input, "command": shlex.join(words)})
            queued = _queue_target(single.command)
            if queued:
                decision = queue_decision(who, state, queued, mode)
                if not decision.allowed:
                    return decision
                continue
            target = _merge_target(words)
            saved = record
            if target:
                urls = [url for url in bound["pulls"] if _same_pull(target, url)]
                if len(urls) != 1:
                    return Decision.deny("Intent requires a pull request registered on this task.")
                saved = pulls.read(_pull_subject(who.task, urls[0]))
                if (
                    not saved
                    or not saved.get("head")
                    or pr_head(urls[0]) != saved["head"]
                    or saved.get("inputs") != bound["inputs"].get(urls[0])
                ):
                    return Decision.deny(
                        "Intent must be checked on this pull request head and contract before it can merge."
                    )
            decision = self._decide_record(single, who, state, mode, saved, bound=True)
            if not decision.allowed:
                return decision
        return Decision()

    def _decide_unbound(self, call, who, state, mode, record):
        commands = [words for words in simple_commands(call.command) if _gated(words)]
        for words in commands:
            single = replace(call, tool_input={**call.tool_input, "command": shlex.join(words)})
            decision = self._decide_record(single, who, state, mode, record)
            if not decision.allowed:
                return decision
        return decision

    def _resolve(self, call, split):
        if not split:
            return call
        named = [self._named(words, call.cwd) for words in simple_commands(call.command)]
        if None in named:
            return None
        return replace(call, tool_input={**call.tool_input, "command": " && ".join(map(shlex.join, named))})

    def _named(self, words, cwd):
        if not _unnamed_merge(words):
            return words
        try:
            done = self.run(_pull_view(words), capture_output=True, text=True, timeout=GH_TIMEOUT_SEC, cwd=cwd or None)
        except (OSError, subprocess.SubprocessError):
            return None
        return [*words, done.stdout.strip()] if done.returncode == 0 and PULL.search(done.stdout) else None

    def _decide_record(self, call, who, state, mode, record, bound=False):
        if not bound and _other_half(call.command, record):
            return Decision.deny(
                f"intent for task {who.task} was judged on {record['url']}, not this pull request. Record this half "
                f"first: agentihooks swarm {who.swarm} pr <pull request url> --half <n>, then wait for its verdict."
            )
        queued = _queue_target(call.command)
        if queued:
            return queue_decision(who, state, queued, mode)
        verdict = record and record["verdict"]
        if mode == "coach" and record and record.get("head"):
            head = pr_head(record["url"])
            if head and head != record["head"]:
                state.write(who.task, PENDING, RUNNING, int(self.clock() * 1000))
                return Decision.deny("Intent must be checked on the new head. Wait for the tick to rerun the check.")
        if verdict == FAIL:
            rounds = record.get("coach_rounds", 0)
            merged = mode == "coach" and bool(record.get("url")) and pr_merged(record["url"])
            if mode == "coach" and (rounds >= 2 or merged):
                outcome = "merged" if merged else "merge permitted"
                unmet = "after two fix rounds" if rounds >= 2 else "on a merged pull request"
                reason = f"{outcome} with intent unmet {unmet}: {record['reason']}"
                log.append(state.slug, log.Row.of(NAME, "count", who, call.tool, reason), state.home)
                return Decision()
            text = f"intent check failed for task {who.task}: {record['reason']}. {fix_steps(who.swarm)}"
            if mode == "coach":
                text += f" Run fix round {rounds + 1} of 2."
            return Decision.deny(text)
        if verdict != PENDING:
            return Decision()
        return self._pending(call, who, state, mode, record)

    def _pending(self, call, who, state, mode, record):
        if record["reason"] == DRAFT:
            if _pr_field(call.command, "draft", self.run) != "false":
                return Decision.deny(
                    f"intent for task {who.task} is judged once its pull request is ready for review. {_next_step(who)}"
                )
            if state.read(who.task) != record:
                return self.decide(call, who, state, mode)
            record = state.write(who.task, PENDING, RUNNING, int(self.clock() * 1000), phase=record.get("phase"))
        waited = int(self.clock() * 1000) - record["at"]
        if waited < GRACE_MS:
            return Decision.deny(
                f"intent check running for task {who.task}, started {waited // 1000} s ago; it passes unchecked at "
                f"{GRACE_MS // 1000} s. {_next_step(who)}"
            )
        reason = "intent check still pending after two minutes, passed unchecked"
        log.append(state.slug, log.Row.of(NAME, "count", who, call.tool, reason), state.home)
        return Decision()
