"""The intent check: the tick asks the classifier whether the phase can use a task's pull request as delivered, and the
intent gate refuses merge and done while that answer is fail, or pending for under two minutes."""

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from hooks.classifier import ClassifierError, code_rules, decide, definitions, runner
from hooks.classifier.questions import MAX_QUESTIONS
from scripts.gates import intent_history, log
from scripts.gates.base import Decision, Who
from scripts.gates.identity import program_index, simple_commands
from scripts.gates.verdicts import Verdicts
from scripts.swarm import timing
from scripts.swarm_ledger import plan_read

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
START, END = "<!-- agentihooks intent -->", "<!-- /agentihooks intent -->"
PULL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
SECTION = re.compile(f"{re.escape(START)}.*?{re.escape(END)}", re.S)
TESTS_FIRST = f"{PURPOSE}-tests-first"
BASE_QUESTIONS = ("usable", "delivers", "reachable", "weakens")
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
    return "Task part: tests-first" in body.splitlines()


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
    return (
        f"{START}\n## Parent intent\n\n**Project:** {doc['overview']}\n\n"
        f"**Phase {phase.get('title', '')}:** {phase.get('description', '')}\n\n"
        f"**Task {task['id']}, {task['title']}:** {task['description']}\n{END}"
    )


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
        body = with_intent(json.loads(read.stdout).get("body") or "", section(doc, task))
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
    if task is not None and planned and planned.get("planned") and planned["verdict"] == PASS:
        if _same_phase(planned, task):
            return {"verdict": PASS, "body": body}
    verdicts.write(task_id, PENDING, RUNNING, now_ms)
    return {"verdict": PENDING, "body": body}


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


def pr_view(url, run=subprocess.run):
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    before = pr_head(url, run)
    if not before:
        return None
    try:
        done = _gh(["gh", "pr", "view", url, "--json", "title,body,files,reviews,comments"], run)
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
            **diff,
            "reviewer_findings": {
                "reviews": raw.get("reviews", []),
                "comments": raw.get("comments", []),
                "inline": [json.loads(line) for line in comments.stdout.splitlines() if line.strip()],
            },
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def _proof_notes(task, proof_chars):
    try:
        return (Path(task["workspace"]) / "proof.md").read_text()[-proof_chars:]
    except (KeyError, OSError):
        return ""


def _plan_chunk(doc, task):
    if not task.get("plan_lines"):
        return {}
    try:
        text = plan_read.exact(doc, task)
    except (ValueError, OSError):
        text = None
    return {"plan_lines": task["plan_lines"], "plan_chunk": text}


def state_of(doc, task, pr, proof_chars=PROOF_CHARS):
    phase = _phase(doc, task)
    return {
        "overview": doc["overview"],
        "phase": phase.get("title", ""),
        "phase_intent": phase.get("description", ""),
        "task": task["title"],
        "task_text": task["description"],
        "pull_request_title": pr["title"],
        "pull_request_body": pr["body"],
        "changed_files": pr["files"],
        "proof": task.get("proof") or {},
        "proof_notes": _proof_notes(task, proof_chars),
        "reviewer_findings": pr.get("reviewer_findings", {}),
        **({"task_part": "tests-first", "pull_request_diff": pr.get("diff")} if _tests_first(pr["body"]) else {}),
        **_plan_chunk(doc, task),
    }


def _rows(state):
    if state.get("task_part") == "tests-first":
        return []
    text = state.get("plan_chunk")
    return plan_read.numbered(text, state["plan_lines"]) if isinstance(text, str) else []


def _definition(state):
    return TESTS_FIRST if state.get("task_part") == "tests-first" else PURPOSE


def _questions(definition, state, params):
    if state.get("task_part") == "tests-first":
        return runner.questions_for(definition)
    rows = _rows(state)
    fits = len(BASE_QUESTIONS) + len(CHUNK_QUESTIONS) + len(rows) <= MAX_QUESTIONS
    asked = runner.questions_for(definition, {**params, "rows": [list(row) for row in rows] if fits else []})
    if not rows:
        return {key: asked[key] for key in BASE_QUESTIONS}
    lines = {f"misses_line_{number}": asked[f"misses_line_{index}"] for index, (number, _) in enumerate(rows) if fits}
    return {**{key: asked[key] for key in (*BASE_QUESTIONS, *CHUNK_QUESTIONS)}, **lines}


def questions_for(state):
    return _questions(definitions.load(_definition(state)), state, {})


def _quoted(rows):
    return ", ".join(f'line {number} "{row}"' for number, row in rows)


def _chunk_reasons(state, answers, thresholds):
    if not _rows(state):
        return []
    lines, under, over = state["plan_lines"], answers["underdelivers"].noul, answers["overdelivers"].noul
    reasons = []
    if under >= thresholds["chunk"]:
        reasons.append(f"the change may leave out something plan lines {lines} ask for, at probability {under:.2f}")
    if over >= thresholds["chunk"]:
        reasons.append(f"the change may add scope plan lines {lines} do not ask for, at probability {over:.2f}")
    return reasons


def _chunk_steps(state, answers, thresholds):
    rows, lines, steps = _rows(state), state.get("plan_lines"), []
    if not rows:
        return steps
    line = thresholds["chunk"]
    if answers["underdelivers"].noul >= line:
        named = [(n, row) for n, row in rows if f"misses_line_{n}" in answers]
        missed = [(n, row) for n, row in named if answers[f"misses_line_{n}"].noul >= line]
        if missed:
            steps.append(f"Deliver what plan lines {lines} ask for and the change leaves out: {_quoted(missed)}.")
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
            return FAIL, "; ".join([*faults, TESTS_FIRST_GUIDANCE])
    usable, weakens, fail = answers["usable"].noul, answers["weakens"].noul, thresholds["fail"]
    chunk = _chunk_reasons(state, answers, thresholds)
    if usable >= fail and weakens < thresholds["weaken"] and not chunk:
        cited = f", judged against plan lines {state['plan_lines']}" if _rows(state) else ""
        return PASS, f"the phase can use it as delivered at probability {usable:.2f}{cited}"
    lead = f"the phase can use this change at probability {usable:.2f}"
    reasons = [text for key, text in REASONS.items() if answers[key].noul < thresholds["reason"]]
    if weakens >= thresholds["weaken"]:
        reasons.append(f"the change may weaken what the phase builds, at probability {weakens:.2f}")
    reasons += chunk
    guidance = remediation(state, answers, thresholds)
    if guidance:
        reasons.append(guidance)
    return FAIL, "; ".join([f"{lead}, under {fail}" if usable < fail else lead, *reasons])


def _verdicts(definition, state, params, answers):
    thresholds = definition.thresholds if definition.name == PURPOSE else definitions.load(PURPOSE).thresholds
    verdict, reason = _verdict(state, answers, thresholds)
    return {"verdict": verdict, "reason": reason}


RULE = code_rules.CodeRule(_questions, _verdicts, {"verdict": (PASS, FAIL, UNCHECKED)}, {"verdict": FAIL})


def judge(state, decide=decide):
    preparatory = state.get("task_part") == "tests-first"
    if preparatory and not isinstance(state.get("pull_request_diff"), str):
        return FAIL, "the tests first part requires a complete pull request diff to judge gate behaviour"
    if not preparatory and state.get("plan_lines") and not isinstance(state.get("plan_chunk"), str):
        return UNCHECKED, f"the plan chunk for lines {state['plan_lines']} could not be read"
    try:
        verdicts = runner.run(_definition(state), state, decider=decide).verdicts
    except definitions.DefinitionError as exc:
        return FAIL, f"the intent definition is invalid: {exc}"
    except ClassifierError:
        return UNCHECKED, "the classifier did not answer"
    return verdicts["verdict"], verdicts["reason"]


def fix_steps(slug: str) -> str:
    return (
        "Fix steps: Deliver the missing parent intent behavior identified in the verdict, "
        "add evidence that the phase can use it as delivered, commit and push the fix, "
        f"then run agentihooks swarm {slug} pr <url> for a new check."
    )


def _fail_kind(mode):
    return "deny" if mode == "enforce" else "observe"


def _remember(slug, task, now_ms, state, answer, home):
    verdict, reason = answer
    record = {
        "task": task["id"],
        "agent": task.get("claimed_by", ""),
        "at": now_ms,
        "purpose": PURPOSE,
        "verdict": verdict,
        "reason": reason,
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
    plan = {
        "title": f"Traced plan for task {task_id}",
        "body": "".join(f"- {row['what']} | {', '.join(row['areas'])} | {row['why']}\n" for row in kept),
        "files": sorted({area for row in kept for area in row["areas"]}),
    }
    state = intent_history.prepare(state_of(doc, task, plan))
    verdict, reason = judge(state)
    _remember(slug, task, now_ms, state, (verdict, reason), home)
    verdicts.write(task_id, verdict, reason, now_ms, phase=task.get("phase"), planned=traced["plan_hash"])
    who = Who(name=task.get("claimed_by", ""), task=task_id)
    if verdict == UNCHECKED:
        log.append(slug, log.Row.of(NAME, "count", who, reason=reason), home)
    elif verdict == FAIL:
        log.append(slug, log.Row.of(NAME, _fail_kind(mode), who, reason=reason), home)
    return {"verdict": verdict, "reason": reason}


@dataclass(frozen=True)
class _Judgment:
    pr: dict | None
    previous: dict | None
    state: dict | None
    answer: tuple[str, str] | None


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

    def run(self, doc):
        if self.mode == "off":
            return []
        verdicts, actions, tasks = Verdicts(self.slug, NAME, self.home), [], []
        for task in doc["tasks"]:
            states = ("pr", "claimed") if self.mode == "coach" else ("pr",)
            if task.get("state") not in states or not task.get("pr_url"):
                continue
            record = verdicts.read(task["id"])
            judged = record and record["verdict"] != PENDING and not record.get("planned")
            if self.mode != "coach" and judged and _same_phase(record, task):
                continue
            tasks.append((task, record))
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [
                (task, record, workers.submit(copy_context().run, self._judge, doc, task)) for task, record in tasks
            ]
            for task, record, future in pending:
                if not record or not _same_phase(record, task):
                    verdicts.write(task["id"], PENDING, RUNNING, self.now_ms, phase=task.get("phase"))
                judgment = future.result()
                if judgment is not None:
                    actions += self._check(task, judgment, verdicts)
                    timing.keep()
        return actions

    def _judge(self, doc, task):
        previous = self._unmoved(task)
        if previous:
            return _Judgment(None, previous, None, None)
        pr = self.view(task["pr_url"])
        if pr is None or (self.mode == "coach" and not pr.get("head")):
            return None
        previous = self._coaching().read(task["id"]) if self.mode == "coach" else None
        previous = previous if previous and _same_phase(previous, task) else None
        if previous and previous["head"] == pr.get("head"):
            return _Judgment(pr, previous, None, None)
        state = intent_history.prepare(state_of(doc, task, pr))
        if state.get("task_part") == "tests-first":
            state["pull_request_diff"] = intent_history.masked(pr.get("diff"))
        return _Judgment(pr, previous, state, self.ask(state))

    def _check(self, task, judgment, verdicts):
        previous, pr = judgment.previous, judgment.pr
        if judgment.state is None:
            self._keep(task, previous, verdicts)
            return []
        rounds = min(previous["coach_rounds"] + (previous["verdict"] == FAIL), 2) if previous else 0
        state, (verdict, reason) = judgment.state, judgment.answer
        head = pr.get("head")
        _remember(self.slug, task, self.now_ms, state, judgment.answer, self.home)
        coached = {"coach_rounds": rounds, "head": head, "url": task["pr_url"]} if self.mode == "coach" else {}
        fields = {"phase": task.get("phase"), **coached}
        verdicts.write(task["id"], verdict, reason, self.now_ms, **fields)
        if self.mode == "coach":
            self._coaching().write(task["id"], verdict, reason, self.now_ms, **fields)
        who = Who(name=task.get("claimed_by", ""), task=task["id"])
        actions = [f"task {task['id']} intent check {verdict}"]
        if verdict == UNCHECKED:
            log.append(self.slug, log.Row.of(NAME, "count", who, reason=reason), self.home)
        elif verdict == FAIL:
            actions += self._failed(task, who, reason, rounds)
        elif self.mode == "coach" and task.get("state") == "claimed":
            self.ledger.update_task(self.slug, task["id"], {"state": "pr"})
        return actions

    def _coaching(self):
        return Verdicts(self.slug, "intent-coach", self.home)

    def _unmoved(self, task):
        if self.mode != "coach" or self.head is None:
            return None
        previous = self._coaching().read(task["id"])
        if (
            not previous
            or not previous.get("head")
            or not _same_phase(previous, task)
            or previous["head"] != self.head(task["pr_url"])
        ):
            return None
        return previous

    def _keep(self, task, previous, verdicts):
        verdicts.write(
            task["id"],
            previous["verdict"],
            previous["reason"],
            previous["at"],
            coach_rounds=previous["coach_rounds"],
            head=previous["head"],
            url=task["pr_url"],
            phase=task.get("phase"),
        )

    def _failed(self, task, who, reason, rounds=0):
        log.append(self.slug, log.Row.of(NAME, _fail_kind(self.mode), who, reason=reason), self.home)
        if self.mode == "coach" and rounds >= 2:
            text = f"Intent remains unmet after two fix rounds: {reason}. The master must review this shortfall."
            key, ref = f"intent-shortfall:{task['id']}:{self.now_ms}", f"tasks/{task['id']}"
            told = self.mail.send(key, self.mail.master, text, ref=ref)
            if task.get("state") == "claimed":
                self.ledger.update_task(self.slug, task["id"], {"state": "pr"})
            self.ledger.comment(self.slug, task["id"], SHORTFALL_COMMENT, by="swarm")
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
    return program == "agentihooks" and rest[:1] == ["swarm"] and rest[2:3] == ["done"]


class IntentGate:
    name = NAME
    default_mode = DEFAULT_MODE

    def __init__(self, clock=time.time):
        self.clock = clock

    def matches(self, call):
        return call.tool == "Bash" and ("merge" in call.command or "done" in call.command)

    def decide(self, call, who, state, mode="enforce"):
        if not (who.pinned and who.task) or not any(map(_gated, simple_commands(call.command))):
            return Decision()
        record = state.read(who.task)
        verdict = record and record["verdict"]
        if mode == "coach" and record and record.get("head"):
            head = pr_head(record["url"])
            if head and head != record["head"]:
                state.write(who.task, PENDING, RUNNING, int(self.clock() * 1000))
                return Decision.deny("Intent must be checked on the new head. Wait for the tick to rerun the check.")
        if verdict == FAIL:
            if mode == "coach" and record.get("coach_rounds", 0) >= 2:
                outcome = "merged" if pr_merged(record["url"]) else "merge permitted"
                reason = f"{outcome} with intent unmet after two fix rounds: {record['reason']}"
                log.append(state.slug, log.Row.of(NAME, "count", who, call.tool, reason), state.home)
                return Decision()
            text = f"intent check failed for task {who.task}: {record['reason']}. {fix_steps(who.swarm)}"
            if mode == "coach":
                text += f" Run fix round {record.get('coach_rounds', 0) + 1} of 2."
            return Decision.deny(text)
        if verdict != PENDING:
            return Decision()
        waited = int(self.clock() * 1000) - record["at"]
        if waited < GRACE_MS:
            return Decision.deny(
                f"intent check running for task {who.task}, started {waited // 1000} s ago; it passes unchecked at "
                f'{GRACE_MS // 1000} s. Wait for it: agentihooks swarm {who.swarm} wait 2 --reason "intent check"'
            )
        reason = "intent check still pending after two minutes, passed unchecked"
        log.append(state.slug, log.Row.of(NAME, "count", who, call.tool, reason), state.home)
        return Decision()
