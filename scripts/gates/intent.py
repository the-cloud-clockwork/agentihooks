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
MODES = ("enforce", "observe", "off")
DEFAULT_MODE = "observe"
PENDING, PASS, FAIL, UNCHECKED = "pending", "pass", "fail", "unchecked"
RUNNING = "intent check running"
FAIL_LINE = 0.3
REASON_LINE = 0.5
GRACE_MS = 2 * 60_000
GH_TIMEOUT_SEC = 20
PROOF_CHARS = 4000
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


def pr_view(url, run=subprocess.run):
    found = PULL.search(url)
    if not found:
        return None
    owner, repo, number = found.groups()
    try:
        done = _gh(["gh", "pr", "view", url, "--json", "title,body,files,reviews,comments"], run)
        raw = json.loads(done.stdout) if done.returncode == 0 else None
        if raw is None:
            return None
        title, body, files = raw["title"], raw["body"] or "", [f["path"] for f in raw["files"]]
        comments = _gh(["gh", "api", "--paginate", "--slurp", f"repos/{owner}/{repo}/pulls/{number}/comments"], run)
        if comments.returncode:
            return None
        return {
            "title": title,
            "body": body,
            "files": files,
            "reviewer_findings": {
                "reviews": raw.get("reviews", []),
                "comments": raw.get("comments", []),
                "inline": [comment for page in json.loads(comments.stdout) for comment in page],
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


def judge(state, decide=decide):
    try:
        answers = decide(state, QUESTIONS, purpose=PURPOSE).answers
    except ClassifierError:
        return UNCHECKED, "the classifier did not answer"
    usable = answers["usable"].noul
    if usable >= FAIL_LINE:
        return PASS, f"the phase can use it as delivered at probability {usable:.2f}"
    reasons = [text for key, text in REASONS.items() if answers[key].noul < REASON_LINE]
    return FAIL, "; ".join([f"the phase can use this change at probability {usable:.2f}, under {FAIL_LINE}", *reasons])


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

    def run(self, doc):
        if self.mode == "off":
            return []
        verdicts, actions = Verdicts(self.slug, NAME, self.home), []
        for task in doc["tasks"]:
            if task.get("state") != "pr" or not task.get("pr_url"):
                continue
            record = verdicts.read(task["id"]) or verdicts.write(task["id"], PENDING, RUNNING, self.now_ms)
            if record["verdict"] != PENDING:
                continue
            pr = self.view(task["pr_url"])
            if pr is None:
                continue
            state = intent_history.prepare(state_of(doc, task, pr))
            classifier_input = intent_history.request(state, QUESTIONS)
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
                    "classifier_input": classifier_input,
                },
                self.home,
            )
            verdicts.write(task["id"], verdict, reason, self.now_ms)
            actions.append(f"task {task['id']} intent check {verdict}")
            who = Who(name=task.get("claimed_by", ""), task=task["id"])
            if verdict == UNCHECKED:
                log.append(self.slug, log.Row.of(NAME, "count", who, reason=reason), self.home)
            elif verdict == FAIL:
                actions += self._failed(task, who, reason)
        return actions

    def _failed(self, task, who, reason):
        kind = "deny" if self.mode == "enforce" else "observe"
        log.append(self.slug, log.Row.of(NAME, kind, who, reason=reason), self.home)
        if self.mode != "enforce":
            return []
        text = (
            f"The intent check failed: {reason}. Deliver the missing piece and run swarm pr again, "
            "or block the task with these reasons."
        )
        self.ledger.update_task(self.slug, task["id"], {"state": "claimed"})
        self.ledger.comment(self.slug, task["id"], text, by="swarm")
        key, ref = f"intent-fail:{task['id']}:{self.now_ms}", f"tasks/{task['id']}"
        return self.mail.send(key, self.mail.engineer(task), text, ref=ref)


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

    def decide(self, call, who, state):
        if not (who.pinned and who.task) or not any(map(_gated, simple_commands(call.command))):
            return Decision()
        record = state.read(who.task)
        verdict = record and record["verdict"]
        if verdict == FAIL:
            return Decision.deny(
                f"intent check failed for task {who.task}: {record['reason']}. Deliver the missing piece, then run "
                f"agentihooks swarm {who.swarm} pr <url> for a new check, or block with "
                f'agentihooks swarm {who.swarm} block "<why>"'
            )
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
