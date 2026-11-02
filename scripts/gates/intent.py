"""The intent check: the tick asks the classifier whether the phase can use a task's pull request as delivered, and the
intent gate refuses merge and done while that answer is fail, or pending for under two minutes."""

import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, cast

from hooks.classifier import ClassifierError, code_rules, decide, definitions, runner
from hooks.classifier.questions import MAX_QUESTIONS
from scripts.gates import intent_history, lift, log
from scripts.gates.base import Decision, Who
from scripts.gates.identity import program_index, simple_commands
from scripts.gates.verdicts import Verdicts
from scripts.swarm import timing
from scripts.swarm_ledger import ledger_splits, plan_read

if TYPE_CHECKING:
    from hooks.classifier.definitions import Definition
    from hooks.classifier.questions import Question
    from hooks.classifier.result import Answer, DecisionResult
    from scripts.swarm.store import SwarmConfig

    class _Ledger(Protocol):
        def update_task(self, slug: str, task_id: str, fields: dict[str, Any]) -> object: ...
        def comment(self, slug: str, task_id: str, text: str, by: str = "") -> object: ...

    class _Mail(Protocol):
        master: str

        def engineer(self, task: dict[str, Any]) -> str: ...
        def send(self, key: str, to: str, text: str, ref: str = "") -> list[str]: ...


type _Runner = Callable[..., subprocess.CompletedProcess[str]]

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


def mode_of(config: "SwarmConfig") -> str:
    chosen = cast(dict[str, Any], config.gates).get(NAME)
    return chosen if chosen in MODES else DEFAULT_MODE


def _tests_first(body: str) -> bool:
    return "Task part: tests-first" in body.splitlines() or bool(
        re.search(
            r"(?:^|[.!?]\s+)(?:This tests[- ]first prerequisite\b|Tests[- ]first prerequisite for #\d+\.(?=\s|$))",
            body,
            re.MULTILINE,
        )
    )


def _pr_diff(url: str, run: _Runner) -> str | None:
    try:
        patch = _gh(["gh", "pr", "diff", url], run)
        return patch.stdout if patch.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _phase(doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    return next((p for p in doc["phases"] if p.get("id") == task.get("phase")), {})


def _same_phase(record: dict[str, Any], task: dict[str, Any]) -> bool:
    return record.get("phase") == task.get("phase")


def section(doc: dict[str, Any], task: dict[str, Any]) -> str:
    phase = _phase(doc, task)
    declared = _declared(task)
    delivers = f"declared {declared['kind']}: {declared['scope']}" if declared else task["description"]
    return (
        f"{START}\n## Parent intent\n\n**Project:** {doc['overview']}\n\n"
        f"**Phase {phase.get('title', '')}:** {phase.get('description', '')}\n\n"
        f"**Task {task['id']}, {task['title']}:** {delivers}\n{END}"
    )


def _declared(task: dict[str, Any]) -> dict[str, Any] | None:
    return cast(dict[str, Any], task.get("pull_scopes") or {}).get(task.get("pr_url"))


def with_intent(body: str, text: str) -> str:
    if SECTION.search(body):
        return SECTION.sub(lambda _: text, body, count=1)
    return f"{body.rstrip()}\n\n{text}" if body.strip() else text


def _gh(args: list[str], run: _Runner, data: str | None = None) -> subprocess.CompletedProcess[str]:
    return run(args, input=data, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC)


def stamp_body(url: str, doc: dict[str, Any], task: dict[str, Any], run: _Runner = subprocess.run) -> bool:
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


def stamp(
    slug: str, task_id: str, url: str, doc: dict[str, Any], mode: str, now_ms: int, home: str | Path | None = None
) -> dict[str, Any]:
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


def _split_fields(task: dict[str, Any] | None, url: str) -> dict[str, Any]:
    return {"url": url, "split": True} if task and task.get("split") else {}


def _pull_subject(task: str, url: str) -> str:
    return f"{task}.{hashlib.sha256(url.encode()).hexdigest()}"


def _bind_pulls(state: Verdicts, task: dict[str, Any] | None, url: str, doc: dict[str, Any]) -> None:
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


def _pr_field(url: str, field: str, run: _Runner) -> str | None:
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    try:
        result = _gh(["gh", "api", f"repos/{owner}/{repo}/pulls/{number}", "--jq", f".{field}"], run)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def pr_merged(url: str, run: _Runner = subprocess.run) -> bool:
    return _pr_field(url, "merged", run) == "true"


def pr_head(url: str, run: _Runner = subprocess.run) -> str | None:
    return _pr_field(url, "head.sha", run)


def production_unchanged(url: str, before: str, after: str, run: _Runner = subprocess.run) -> bool:
    found = PULL.search(url)
    if not found:
        return False
    owner, repo, _ = found.groups()
    compare = ["gh", "api", f"repos/{owner}/{repo}/compare/{before}...{after}", "--jq", COMPARED]
    try:
        result = _gh(compare, run)
        status, files = cast(
            "tuple[str | None, list[str]]", json.loads(result.stdout) if result.returncode == 0 else (None, [])
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return status == "ahead" and len(files) < COMPARE_FILES and all(path.startswith(NON_PRODUCTION) for path in files)


def text_key(title: str, body: str | None) -> str:
    return hashlib.sha256(json.dumps([title, body or ""]).encode()).hexdigest()


def pr_text(url: str, run: _Runner = subprocess.run) -> str | None:
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    try:
        result = _gh(["gh", "api", f"repos/{owner}/{repo}/pulls/{number}", "--jq", "{title, body}"], run)
        raw = cast("dict[str, Any] | None", json.loads(result.stdout) if result.returncode == 0 else None)
        return text_key(raw["title"], raw["body"]) if raw else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def pr_view(url: str, run: _Runner = subprocess.run) -> dict[str, Any] | None:
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    before = pr_head(url, run)
    if not before:
        return None
    try:
        done = _gh(["gh", "pr", "view", url, "--json", "title,body,files,reviews,comments,isDraft,state"], run)
        raw = cast("dict[str, Any] | None", json.loads(done.stdout) if done.returncode == 0 else None)
        if raw is None:
            return None
        title, body, files = raw["title"], raw["body"] or "", [f["path"] for f in raw["files"]]
        path = f"repos/{owner}/{repo}/pulls/{number}/comments"
        comments = _gh(["gh", "api", "--paginate", "--jq", ".[] | @json", path], run)
        if comments.returncode:
            return None
        diff: dict[str, Any] = {}
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


def named_pulls(task: dict[str, Any], body: str) -> list[str]:
    own = PULL.search(task["pr_url"])
    here = (own[1].lower(), own[2].lower()) if own else None
    texts = [task.get("description"), SECTION.sub("", body), *(c.get("text") for c in task.get("comments") or [])]
    urls: list[str] = []
    seen: set[str] = set()
    for text in filter(None, texts):
        short = (m.groups() for m in SHORT.finditer(text) if here and m[1].lower() == here[0])
        for owner, repo, number in [*(m.groups() for m in PULL.finditer(text)), *short]:
            url = f"https://github.com/{owner}/{repo}/pull/{number}"
            if (owner.lower(), repo.lower()) != here and url.lower() not in seen:
                seen.add(url.lower())
                urls.append(url)
    return urls


def named_view(url: str, run: _Runner = subprocess.run) -> dict[str, Any] | None:
    try:
        done = _gh(["gh", "pr", "view", url, "--json", "title,body,files,state"], run)
        raw = cast("dict[str, Any] | None", json.loads(done.stdout) if done.returncode == 0 else None)
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


def _proof_notes(task: dict[str, Any], proof_chars: int) -> str:
    try:
        return (Path(task["workspace"]) / "proof.md").read_text()[-proof_chars:]
    except (KeyError, OSError):
        return ""


def _split_half(task: dict[str, Any]) -> tuple[int, Any] | None:
    halves, pulls = task.get("split") or [], task.get("split_pulls") or []
    index = pulls.index(task.get("pr_url")) if task.get("pr_url") in pulls else len(pulls)
    return (index, halves[index]) if index < len(halves) else None


def _split_note(task: dict[str, Any]) -> str:
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


def _plan_chunk(doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    half = _split_half(task) if task.get("split") and ledger_splits.ranged(task["split"]) else None
    task = {**task, "plan_lines": half[1]} if half else task
    if not task.get("plan_lines") or task.get("follow_up") or _declared(task):
        return {}
    try:
        text = plan_read.exact(doc, task)
    except (ValueError, OSError):
        text = None
    part: dict[str, Any] = {"plan_lines": task["plan_lines"], "plan_chunk": text}
    if task.get("plan_slice") != task.get("id"):
        part = cast(dict[str, Any], plan_read.scoped(doc, task, part))
    if "shared_slice" not in part and isinstance(text, str) and _foreign_slice(doc, task, text):
        return {}
    return part


def _foreign_slice(doc: dict[str, Any], task: dict[str, Any], text: str) -> bool:
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


def _parent(doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    phase = _phase(doc, task)
    return {
        "overview": doc["overview"],
        "phase": phase.get("title", ""),
        "phase_intent": phase.get("description", ""),
        "task": task["title"],
        "task_text": _task_text(task),
    }


def _task_text(task: dict[str, Any]) -> str:
    declared = _declared(task)
    if declared is None:
        return task["description"] + _split_note(task)
    return (
        f"This pull request is a declared {declared['kind']} of this task and delivers only: {declared['scope']}. "
        "The whole task is judged at its final pull request."
    )


def _task_contract(doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    phase = _phase(doc, task)
    if task.get("split") and not ledger_splits.ranged(task["split"]):
        return {**task, "plan_lines": ""}
    if task.get("split") and (
        task.get("follow_up") or not (task.get("plan_url") or phase.get("plan_url") or phase.get("plan_ref"))
    ):
        return {**task, "split": [], "plan_lines": ""}
    return task


def state_of(
    doc: dict[str, Any], task: dict[str, Any], pr: dict[str, Any], proof_chars: int = PROOF_CHARS
) -> dict[str, Any]:
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


def plan_state(doc: dict[str, Any], task: dict[str, Any], kept: list[dict[str, Any]]) -> dict[str, Any]:
    task = _task_contract(doc, task)
    return {
        "stage": "plan",
        **_parent(doc, task),
        "planned_pieces": [f"{row['what']} | {', '.join(row['areas'])} | {row['why']}" for row in kept],
        "planned_areas": sorted({area for row in kept for area in row["areas"]}),
        **_plan_chunk(doc, task),
    }


def _named_state(pr: dict[str, Any]) -> dict[str, Any]:
    if "pull_requests" not in pr:
        return {}
    pulls = pr["pull_requests"]
    return {
        "pull_requests": pulls,
        "judged_pull_requests": [p["url"] for p in pulls],
        "unread_pull_requests": pr["unread_pull_requests"],
        **({"unread_overflow": pr["unread_overflow"]} if "unread_overflow" in pr else {}),
    }


def _listed(urls: list[str], more: int) -> str:
    more += len(urls[LISTED:])
    if more:
        return f"{', '.join(urls[:LISTED])} and {more} more"
    return f"{', '.join(urls[:-1])} and {urls[-1]}" if len(urls) > 1 else urls[0]


def _judged_on(state: dict[str, Any]) -> str:
    judged, unread = state.get("judged_pull_requests") or [], state.get("unread_pull_requests") or []
    text = f"; judged on {_listed(judged, 0)}" if judged else ""
    return text + (f"; could not read {_listed(unread, state.get('unread_overflow', 0))}" if unread else "")


def _pending(pr: dict[str, Any]) -> dict[str, Any]:
    return {"named_pending": pr["named_pending"]} if pr.get("named_pending") else {}


def _rows(state: dict[str, Any]) -> list[tuple[int, str]]:
    if state.get("task_part") == "tests-first":
        return []
    text = state.get("plan_chunk")
    return plan_read.numbered(text, state["plan_lines"]) if isinstance(text, str) else []


def _stage(state: dict[str, Any]) -> str:
    if state.get("task_part") == "tests-first":
        return "tests-first"
    return "plan" if state.get("stage") == "plan" else "delivered"


def _definition(state: dict[str, Any]) -> str:
    return {"tests-first": TESTS_FIRST, "plan": PLAN}.get(_stage(state), PURPOSE)


def _questions(definition: "Definition", state: object, params: dict[str, Any]) -> dict[str, "Question"]:
    state = cast(dict[str, Any], state)
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


def questions_for(state: dict[str, Any]) -> dict[str, "Question"]:
    return _questions(definitions.load(_definition(state)), state, {})


def _quoted(rows: list[tuple[int, str]]) -> str:
    return ", ".join(f'line {number} "{row}"' for number, row in rows)


def _chunk_reasons(
    state: dict[str, Any], answers: dict[str, "Answer"], thresholds: dict[str, float], subject: str = "the change"
) -> list[str]:
    if not _rows(state):
        return []
    lines = state["plan_lines"]
    under, over = cast(float, answers["underdelivers"].noul), cast(float, answers["overdelivers"].noul)
    reasons: list[str] = []
    if under >= thresholds["chunk"]:
        reasons.append(f"{subject} may leave out something plan lines {lines} ask for, at probability {under:.2f}")
    if over >= thresholds["chunk"]:
        reasons.append(f"{subject} may add scope plan lines {lines} do not ask for, at probability {over:.2f}")
    return reasons


def _chunk_steps(
    state: dict[str, Any], answers: dict[str, "Answer"], thresholds: dict[str, float], subject: str = "the change"
) -> list[str]:
    rows, lines = _rows(state), state.get("plan_lines")
    steps: list[str] = []
    if not rows:
        return steps
    line = thresholds["chunk"]
    if cast(float, answers["underdelivers"].noul) >= line:
        named = [(n, row) for n, row in rows if f"misses_line_{n}" in answers]
        missed = [(n, row) for n, row in named if cast(float, answers[f"misses_line_{n}"].noul) >= line]
        if missed:
            steps.append(f"Deliver what plan lines {lines} ask for and {subject} leaves out: {_quoted(missed)}.")
        else:
            steps.append(
                f"Deliver what plan lines {lines} ask for. No single line was named, so check each: {_quoted(rows)}."
            )
    if cast(float, answers["overdelivers"].noul) >= line:
        steps.append(f"Remove the scope beyond plan lines {lines}, which ask only for {_quoted(rows)}.")
    return steps


def remediation(state: dict[str, Any], answers: dict[str, "Answer"], thresholds: dict[str, float]) -> str:
    if state.get("task_part") == "tests-first":
        return TESTS_FIRST_GUIDANCE
    steps: list[str] = []
    if state.get("task_text"):
        task = f"{state['task']}: {state['task_text']}"
        phase = f"{state['phase']}: {state['phase_intent']}"
        steps.append(f"Deliver {task}. The phase must be able to use it for {phase}.")
        if cast(float, answers["delivers"].noul) < thresholds["reason"]:
            steps.append(f"Implement the missing acceptance behavior described by {task}.")
        if cast(float, answers["reachable"].noul) < thresholds["reason"]:
            steps.append(
                f"Wire the production entrypoint for {state['task']} and prove an invocation delivers {phase}."
            )
        if cast(float, answers["weakens"].noul) >= thresholds["weaken"]:
            steps.append(f"Preserve {phase} while implementing {task}.")
    steps += _chunk_steps(state, answers, thresholds)
    return "What would meet intent: " + " ".join(steps) if steps else ""


def _verdict(
    state: dict[str, Any], answers: dict[str, "Answer"], thresholds: dict[str, float]
) -> tuple[str, str, str | None]:
    if state.get("task_part") == "tests-first":
        faults = []
        if cast(float, answers["accepts_both_states"].noul) < thresholds["reason"]:
            faults.append("the preparatory tests may not accept both old and new gate states")
        if cast(float, answers["changes_gate_behavior"].noul) >= thresholds["weaken"]:
            faults.append("the tests first part may change gate behaviour")
        if faults:
            return FAIL, "; ".join([*faults, TESTS_FIRST_GUIDANCE]), "tests-first"
    if state.get("stage") == "plan":
        return _plan_verdict(state, answers, thresholds)
    usable = cast(float, answers["usable"].noul)
    weakens = cast(float, answers["weakens"].noul)
    fail = thresholds["fail"]
    chunk = _chunk_reasons(state, answers, thresholds)
    if usable >= fail and weakens < thresholds["weaken"] and not chunk:
        cited = f", judged against plan lines {state['plan_lines']}" if _rows(state) else ""
        return PASS, f"the phase can use it as delivered at probability {usable:.2f}{cited}", None
    lead, criterion = _delivered_lead(answers, thresholds)
    reasons = [text for key, text in REASONS.items() if cast(float, answers[key].noul) < thresholds["reason"]]
    if weakens >= thresholds["weaken"]:
        reasons.append(f"the change may weaken what the phase builds, at probability {weakens:.2f}")
    reasons += chunk
    guidance = remediation(state, answers, thresholds)
    if guidance:
        reasons.append(guidance)
    return FAIL, "; ".join([lead, *reasons]), criterion


def _delivered_lead(answers: dict[str, "Answer"], thresholds: dict[str, float]) -> tuple[str, str]:
    usable, fail = cast(float, answers["usable"].noul), thresholds["fail"]
    if usable >= fail:
        weakened = cast(float, answers["weakens"].noul) >= thresholds["weaken"]
        return f"the phase can use this change at probability {usable:.2f}", "weakens" if weakened else "chunk"
    if "aligned" not in answers:
        return f"the phase can use this change at probability {usable:.2f}, under {fail}", "usable"
    aligned = cast(float, answers["aligned"].noul)
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


def _plan_verdict(
    state: dict[str, Any], answers: dict[str, "Answer"], thresholds: dict[str, float]
) -> tuple[str, str, str | None]:
    aligned, covers, weakens = (cast(float, answers[key].noul) for key in PLAN_QUESTIONS)
    fail = thresholds["fail"]
    faults: list[tuple[str, str]] = []
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


def _verdicts(
    definition: "Definition", state: object, params: dict[str, Any], answers: dict[str, "Answer"]
) -> dict[str, Any]:
    state = cast(dict[str, Any], state)
    thresholds = definition.thresholds if definition.name == PURPOSE else definitions.load(PURPOSE).thresholds
    verdict, reason, criterion = _verdict(state, answers, thresholds)
    shared = f"; {state['shared_slice']}" if state.get("shared_slice") else ""
    return {"verdict": verdict, "reason": reason + _judged_on(state) + shared, "criterion": criterion}


RULE = code_rules.CodeRule(_questions, _verdicts, {"verdict": (PASS, FAIL, UNCHECKED)}, {"verdict": FAIL})


def judged(state: dict[str, Any], decide: Callable[..., "DecisionResult"] = decide) -> dict[str, Any]:
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


def judge(state: dict[str, Any], decide: Callable[..., "DecisionResult"] = decide) -> tuple[str, str]:
    answer = judged(state, decide)
    return answer["verdict"], answer["reason"]


def fix_steps(slug: str) -> str:
    return (
        "Fix steps: Deliver the missing parent intent behavior identified in the verdict, "
        "add evidence that the phase can use it as delivered, commit and push the fix, "
        f"then run agentihooks swarm {slug} pr <url> for a new check."
    )


def _fail_kind(mode: str) -> str:
    return "deny" if mode == "enforce" else "observe"


def _answer(value: dict[str, Any] | tuple[str, str] | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {"verdict": value[0], "reason": value[1]}


def _detail(answer: dict[str, Any]) -> dict[str, Any]:
    return {key: answer[key] for key in DETAIL if key in answer}


def _remember(
    slug: str, task: dict[str, Any], now_ms: int, state: dict[str, Any], answer: dict[str, Any], home: str | Path | None
) -> None:
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


def plan_check(
    slug: str,
    doc: dict[str, Any],
    task_id: str,
    traced: dict[str, Any],
    mode: str,
    now_ms: int,
    home: str | Path | None = None,
) -> dict[str, Any] | None:
    task = next((t for t in doc["tasks"] if t.get("id") == task_id), None)
    if mode == "off" or task is None or task.get("pr_url"):
        return None
    verdicts = Verdicts(slug, NAME, home)
    previous = verdicts.read(task_id)
    inputs = _fingerprint(doc, task)
    if (
        previous
        and previous.get("planned") == traced["plan_hash"]
        and previous.get("inputs") == inputs
        and _same_phase(previous, task)
    ):
        return {"verdict": previous["verdict"], "reason": previous["reason"]}
    kept = [row for row in traced["pieces"] if row["kept"]]
    state = cast(dict[str, Any], intent_history.prepare(plan_state(doc, task, kept)))
    answer = judged(state)
    verdict, reason = answer["verdict"], answer["reason"]
    _remember(slug, task, now_ms, state, answer, home)
    fields = {"phase": task.get("phase"), "planned": traced["plan_hash"], "inputs": inputs, **_detail(answer)}
    verdicts.write(task_id, verdict, reason, now_ms, **fields)
    who = Who(name=task.get("claimed_by", ""), task=task_id)
    if verdict == UNCHECKED:
        log.append(slug, log.Row.of(NAME, "count", who, reason=reason), home)
    elif verdict == FAIL:
        log.append(slug, log.Row.of(NAME, _fail_kind(mode), who, reason=reason), home)
    return {"verdict": verdict, "reason": reason}


def _fingerprint(doc: dict[str, Any], task: dict[str, Any]) -> str:
    phase = _phase(doc, task)
    parent = [task.get("title"), task.get("description"), phase.get("title"), phase.get("description")]
    declared = _declared(task)
    inputs = [*parent, _plan_chunk(doc, task), *([declared] if declared else [])]
    return hashlib.sha256(json.dumps(inputs).encode()).hexdigest()


def _drafted(record: dict[str, Any] | None) -> bool:
    return bool(record) and record["verdict"] == PENDING and record["reason"] == DRAFT


@dataclass(frozen=True)
class _Judgment:
    pr: dict[str, Any] | None
    previous: dict[str, Any] | None
    state: dict[str, Any] | None
    answer: dict[str, Any] | tuple[str, str] | None
    inputs: str | None = None


@dataclass(frozen=True)
class Check:
    slug: str
    mode: str
    now_ms: int
    ledger: "_Ledger"
    mail: "_Mail"
    view: Callable[[str], dict[str, Any] | None]
    ask: Callable[[dict[str, Any]], dict[str, Any] | tuple[str, str]]
    home: str | Path | None = None
    head: Callable[[str], str | None] | None = None
    named: Callable[[str], dict[str, Any] | None] | None = None
    text: Callable[[str], str | None] | None = None

    def run(self, doc: dict[str, Any]) -> list[str]:
        if self.mode == "off":
            return []
        verdicts, actions, tasks = (
            Verdicts(self.slug, NAME, self.home),
            cast(list[str], []),
            cast(list[tuple[dict[str, Any], dict[str, Any] | None, object]], []),
        )
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

    def _queue(
        self,
        doc: dict[str, Any],
        task: dict[str, Any],
        verdicts: Verdicts,
        tasks: list[tuple[dict[str, Any], dict[str, Any] | None, object]],
    ) -> list[str]:
        _bind_pulls(verdicts, task, task["pr_url"], doc)
        record = verdicts.read(task["id"])
        judged = record and record["verdict"] != PENDING and not record.get("planned")
        moved = (
            self.mode != "coach"
            and judged
            and (
                cast(dict[str, Any], record).get("inputs") != _fingerprint(doc, task)
                or self._named_moved(cast(dict[str, Any], record))
            )
        )
        if self.mode != "coach" and judged and _same_phase(cast(dict[str, Any], record), task) and not moved:
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

    def _isolated(self, task: dict[str, Any], step: Callable[..., list[str]], *args: Any) -> list[str]:
        from scripts.swarm.ledger_client import LedgerRefused

        try:
            return step(*args)
        except LedgerRefused:
            raise
        except Exception as exc:  # one task's failure is logged and never ends the tick run
            who = Who(name=task.get("claimed_by", ""), task=task["id"])
            log.append(self.slug, log.Row.of(NAME, "fail-open", who, reason=f"{type(exc).__name__}: {exc}"), self.home)
            return [f"task {task['id']} intent check failed: {type(exc).__name__}"]

    def _settle(
        self,
        task: dict[str, Any],
        record: dict[str, Any] | None,
        moved: object,
        future: Any,
        verdicts: Verdicts,
    ) -> list[str]:
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
                cast(dict[str, Any], record)["at"],
                phase=cast(dict[str, Any], record).get("phase"),
                **_split_fields(task, task["pr_url"]),
            )
        if judgment is None:
            return []
        actions = self._check(task, judgment, verdicts)
        timing.keep()
        return actions

    def _judge(self, doc: dict[str, Any], task: dict[str, Any]) -> "_Judgment | None":
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
        state = cast(dict[str, Any], intent_history.prepare(state_of(doc, task, pr)))
        if state.get("task_part") == "tests-first":
            state["pull_request_diff"] = intent_history.masked(pr.get("diff"))
        return _Judgment(pr, previous, state, self.ask(state), inputs)

    def _with_named(self, task: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
        others = named_pulls(task, pr["body"]) if self.named else []
        if not others:
            return pr
        named = cast(Callable[[str], dict[str, Any] | None], self.named)
        views = {url: named(url) for url in [task["pr_url"], *others[:NAMED_LIMIT]]}
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

    def _named_moved(self, record: dict[str, Any]) -> bool:
        pending = record.get("named_pending") or []
        named = cast(Callable[[str], dict[str, Any] | None], self.named)
        return bool(self.named) and any((view := named(url)) and view["merged"] for url in pending)

    def _check(self, task: dict[str, Any], judgment: "_Judgment", verdicts: Verdicts) -> list[str]:
        previous, pr = judgment.previous, judgment.pr
        if judgment.state is None and previous is None:
            verdicts.write(
                task["id"], PENDING, DRAFT, self.now_ms, phase=task.get("phase"), **_split_fields(task, task["pr_url"])
            )
            return []
        if judgment.state is None:
            self._keep(task, cast(dict[str, Any], previous), verdicts)
            return []
        pr = cast(dict[str, Any], pr)
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

    def _coaching(self) -> Verdicts:
        return Verdicts(self.slug, "intent-coach", self.home)

    def _unmoved(self, task: dict[str, Any], inputs: str) -> dict[str, Any] | None:
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

    def _text_moved(self, previous: dict[str, Any], url: str) -> bool:
        if self.text is None:
            return False
        current = self.text(url)
        return current is None or previous.get("text") != current

    def _keep(self, task: dict[str, Any], previous: dict[str, Any], verdicts: Verdicts) -> None:
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

    def _save_pull(self, task: dict[str, Any], record: dict[str, Any]) -> None:
        if task.get("split"):
            fields = {key: value for key, value in record.items() if key not in ("verdict", "reason", "at")}
            Verdicts(self.slug, f"{NAME}-pulls", self.home).write(
                _pull_subject(task["id"], task["pr_url"]),
                record["verdict"],
                record["reason"],
                record["at"],
                **fields,
            )

    def _failed(self, task: dict[str, Any], who: Who, reason: str, rounds: int = 0, merged: bool = False) -> list[str]:
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


def _gated(words: list[str]) -> bool:
    index = program_index(words)
    if index is None:
        return False
    program, rest = PurePosixPath(words[index]).name, words[index + 1 :]
    if program == "gh":
        return rest[:2] == ["pr", "merge"]
    return program == "agentihooks" and rest[:1] == ["swarm"] and (rest[2:3] in RELEASES or rest[2:4] == MERGE_QUEUE)


def _merge_target(words: list[str]) -> str | None:
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


def _unnamed_merge(words: list[str]) -> bool:
    index = program_index(words)
    return (
        index is not None
        and PurePosixPath(words[index]).name == "gh"
        and words[index + 1 : index + 3] == ["pr", "merge"]
        and _merge_target(words) is None
    )


def _pull_view(words: list[str]) -> list[str]:
    index = program_index(words)
    rest, branch, repo = iter(words[cast(int, index) + 3 :]), [], []
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


def _same_pull(target: str, url: str) -> bool:
    named, bound = PULL.search(target), PULL.search(url)
    return bool(bound) and (named.groups() if named else target) == (bound.groups() if named else bound[3])


def _queue_target(command: str) -> str | None:
    for words in simple_commands(command):
        index = program_index(words)
        if PurePosixPath(words[cast(int, index)]).name == "agentihooks":
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


def _other_half(command: str, record: dict[str, Any] | None) -> bool:
    judged = record and record.get("split") and PULL.search(record["url"])
    if not judged:
        return False
    for target in filter(None, map(_merge_target, simple_commands(command))):
        named = PULL.search(target)
        if (named.groups() if named else target) != (judged.groups() if named else judged[3]):
            return True
    return False


def _next_step(who: Who) -> str:
    return (
        "Mark the pull request ready with gh pr ready <pull request url> if it is a draft, then wait for the verdict: "
        f"agentihooks swarm {who.swarm} wait --on intent <pull request url>"
    )


class IntentGate:
    name = NAME
    default_mode = DEFAULT_MODE

    def __init__(self, clock: Callable[[], float] = time.time, run: _Runner = subprocess.run) -> None:
        self.clock, self.run = clock, run

    def matches(self, call: Any) -> bool:
        return call.tool == "Bash" and any(word in call.command for word in ("merge", "done", "ready"))

    def decide(self, call: Any, who: Who, state: Verdicts, mode: str = "enforce") -> Decision:
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

    def _decide_unbound(
        self, call: Any, who: Who, state: Verdicts, mode: str, record: dict[str, Any] | None
    ) -> Decision:
        commands = [words for words in simple_commands(call.command) if _gated(words)]
        decision = Decision()
        for words in commands:
            single = replace(call, tool_input={**call.tool_input, "command": shlex.join(words)})
            decision = self._decide_record(single, who, state, mode, record)
            if not decision.allowed:
                return decision
        return decision

    def _resolve(self, call: Any, split: Any) -> Any:
        if not split:
            return call
        named = [self._named(words, call.cwd) for words in simple_commands(call.command)]
        if None in named:
            return None
        return replace(call, tool_input={**call.tool_input, "command": " && ".join(map(shlex.join, named))})

    def _named(self, words: list[str], cwd: str) -> list[str] | None:
        if not _unnamed_merge(words):
            return words
        try:
            done = self.run(_pull_view(words), capture_output=True, text=True, timeout=GH_TIMEOUT_SEC, cwd=cwd or None)
        except (OSError, subprocess.SubprocessError):
            return None
        return [*words, done.stdout.strip()] if done.returncode == 0 and PULL.search(done.stdout) else None

    def _decide_record(
        self, call: Any, who: Who, state: Verdicts, mode: str, record: dict[str, Any] | None, bound: bool = False
    ) -> Decision:
        if not bound and _other_half(call.command, record):
            record = cast(dict[str, Any], record)
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
            record = cast(dict[str, Any], record)
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
        return self._pending(call, who, state, mode, cast(dict[str, Any], record))

    def _pending(self, call: Any, who: Who, state: Verdicts, mode: str, record: dict[str, Any]) -> Decision:
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
