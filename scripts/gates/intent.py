"""The intent check: the tick asks the classifier whether the phase can use a task's pull request as delivered, and the
intent gate refuses merge and done while that answer is fail, or pending for under two minutes."""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from hooks.classifier import ClassifierError, YesNo, decide
from scripts.gates import intent_history, log
from scripts.gates.base import Decision, Who
from scripts.gates.identity import program_index, simple_commands
from scripts.gates.verdicts import Verdicts

NAME = "intent"
PURPOSE = "intent-check"
MODES = ("enforce", "observe", "off", "coach")
DEFAULT_MODE = "observe"
PENDING, PASS, FAIL, UNCHECKED = "pending", "pass", "fail", "unchecked"
RUNNING = "intent check running"
FAIL_LINE = 0.3
REASON_LINE = 0.5
WEAKEN_LINE = 0.5
GRACE_MS = 2 * 60_000
GH_TIMEOUT_SEC = 20
PROOF_CHARS = 4000
FAIL_COMMENT = "The intent check failed. The engineer has the verdict and the fix steps in the inbox."
SHORTFALL_COMMENT = "Intent remains unmet after two fix rounds. The master must review this shortfall in the gate log."
START, END = "<!-- agentihooks intent -->", "<!-- /agentihooks intent -->"
PULL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
SECTION = re.compile(f"{re.escape(START)}.*?{re.escape(END)}", re.S)
QUESTIONS = {
    "usable": YesNo(
        "Can the phase use this change as delivered, given the project overview, the phase intent and the task?",
        true="the phase can use it as delivered",
        false="the phase cannot use it as delivered",
    ),
    "delivers": YesNo(
        "Does the change deliver what the task text asks for?",
        true="it delivers the task text",
        false="part of the task text is missing",
    ),
    "reachable": YesNo(
        "Can the people or agents the phase serves reach the change through something it ships, such as a command, "
        "a hook, a page or a call site?",
        true="something in the change reaches it",
        false="nothing in the change reaches it",
    ),
    "weakens": YesNo(
        "Does the change turn off, loosen, weaken or bypass anything the phase builds, such as a gate default, a "
        "threshold, a check, a published record or a review step?",
        true="the change weakens what the phase builds",
        false="the change weakens nothing the phase builds",
    ),
}
REASONS = {
    "delivers": "the change may not deliver what the task text asks",
    "reachable": "nothing in the change may let the phase reach it",
}


def mode_of(config):
    chosen = config.gates.get(NAME)
    return chosen if chosen in MODES else DEFAULT_MODE


def _phase(doc, task):
    return next((p for p in doc["phases"] if p.get("id") == task.get("phase")), {})


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
    Verdicts(slug, NAME, home).write(task_id, PENDING, RUNNING, now_ms)
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
        head = pr_head(url, run)
        if not head or head != before:
            return None
        return {
            "head": head,
            "title": title,
            "body": body,
            "files": files,
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
    }


def remediation(state: dict, answers: dict) -> str:
    if not state.get("task_text"):
        return ""
    task = f"{state['task']}: {state['task_text']}"
    phase = f"{state['phase']}: {state['phase_intent']}"
    steps = [f"Deliver {task}. The phase must be able to use it for {phase}."]
    if answers["delivers"].noul < REASON_LINE:
        steps.append(f"Implement the missing acceptance behavior described by {task}.")
    if answers["reachable"].noul < REASON_LINE:
        steps.append(f"Wire the production entrypoint for {state['task']} and prove an invocation delivers {phase}.")
    if answers["weakens"].noul >= WEAKEN_LINE:
        steps.append(f"Preserve {phase} while implementing {task}.")
    return "What would meet intent: " + " ".join(steps)


def judge(state, decide=decide):
    try:
        answers = decide(state, QUESTIONS, purpose=PURPOSE).answers
    except ClassifierError:
        return UNCHECKED, "the classifier did not answer"
    usable, weakens = answers["usable"].noul, answers["weakens"].noul
    if usable >= FAIL_LINE and weakens < WEAKEN_LINE:
        return PASS, f"the phase can use it as delivered at probability {usable:.2f}"
    lead = f"the phase can use this change at probability {usable:.2f}"
    reasons = [text for key, text in REASONS.items() if answers[key].noul < REASON_LINE]
    if weakens >= WEAKEN_LINE:
        reasons.append(f"the change may weaken what the phase builds, at probability {weakens:.2f}")
    guidance = remediation(state, answers)
    if guidance:
        reasons.append(guidance)
    return FAIL, "; ".join([f"{lead}, under {FAIL_LINE}" if usable < FAIL_LINE else lead, *reasons])


def fix_steps(slug: str) -> str:
    return (
        "Fix steps: Deliver the missing parent intent behavior identified in the verdict, "
        "add evidence that the phase can use it as delivered, commit and push the fix, "
        f"then run agentihooks swarm {slug} pr <url> for a new check."
    )


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
        verdicts, actions = Verdicts(self.slug, NAME, self.home), []
        for task in doc["tasks"]:
            states = ("pr", "claimed") if self.mode == "coach" else ("pr",)
            if task.get("state") not in states or not task.get("pr_url"):
                continue
            record = verdicts.read(task["id"]) or verdicts.write(task["id"], PENDING, RUNNING, self.now_ms)
            if self.mode != "coach" and record["verdict"] != PENDING:
                continue
            if self._unmoved(task, verdicts):
                continue
            pr = self.view(task["pr_url"])
            if pr is not None:
                actions += self._check(doc, task, pr, verdicts)
        return actions

    def _check(self, doc, task, pr, verdicts):
        coaching = Verdicts(self.slug, "intent-coach", self.home)
        previous = coaching.read(task["id"]) if self.mode == "coach" else None
        head = pr.get("head")
        if self.mode == "coach" and not head:
            return []
        if previous and previous["head"] == head:
            self._keep(task, previous, verdicts)
            return []
        rounds = min(previous["coach_rounds"] + (previous["verdict"] == FAIL), 2) if previous else 0
        state = intent_history.prepare(state_of(doc, task, pr))
        verdict, reason = self.ask(state)
        intent_history.append(
            self.slug,
            {
                "task": task["id"],
                "agent": task.get("claimed_by", ""),
                "at": self.now_ms,
                "purpose": PURPOSE,
                "verdict": verdict,
                "reason": reason,
                "classifier_input": intent_history.request(state, QUESTIONS),
            },
            self.home,
        )
        fields = {"coach_rounds": rounds, "head": head, "url": task["pr_url"]} if self.mode == "coach" else {}
        verdicts.write(task["id"], verdict, reason, self.now_ms, **fields)
        if self.mode == "coach":
            coaching.write(task["id"], verdict, reason, self.now_ms, **fields)
        who = Who(name=task.get("claimed_by", ""), task=task["id"])
        actions = [f"task {task['id']} intent check {verdict}"]
        if verdict == UNCHECKED:
            log.append(self.slug, log.Row.of(NAME, "count", who, reason=reason), self.home)
        elif verdict == FAIL:
            actions += self._failed(task, who, reason, rounds)
        elif self.mode == "coach" and task.get("state") == "claimed":
            self.ledger.update_task(self.slug, task["id"], {"state": "pr"})
        return actions

    def _unmoved(self, task, verdicts):
        if self.mode != "coach" or self.head is None:
            return False
        previous = Verdicts(self.slug, "intent-coach", self.home).read(task["id"])
        if not previous or not previous.get("head") or previous["head"] != self.head(task["pr_url"]):
            return False
        self._keep(task, previous, verdicts)
        return True

    def _keep(self, task, previous, verdicts):
        verdicts.write(
            task["id"],
            previous["verdict"],
            previous["reason"],
            previous["at"],
            coach_rounds=previous["coach_rounds"],
            head=previous["head"],
            url=task["pr_url"],
        )

    def _failed(self, task, who, reason, rounds=0):
        kind = "deny" if self.mode == "enforce" else "observe"
        log.append(self.slug, log.Row.of(NAME, kind, who, reason=reason), self.home)
        if self.mode == "coach" and rounds >= 2:
            text = f"Intent remains unmet after two fix rounds: {reason}. The master must review this shortfall."
            key, ref = f"intent-shortfall:{task['id']}:{self.now_ms}", f"tasks/{task['id']}"
            told = self.mail.send(key, self.mail.master, text, ref=ref)
            self.ledger.comment(self.slug, task["id"], SHORTFALL_COMMENT, by="swarm")
            if task.get("state") == "claimed":
                self.ledger.update_task(self.slug, task["id"], {"state": "pr"})
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
