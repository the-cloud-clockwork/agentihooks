"""The Doctor master loop: a detection pass per interval, judged findings to tasks, and the close after quiet hours."""

import hashlib
import json
import os

from scripts.doctor.words import plural
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.health.findings import MINUTE_MS, limits
from scripts.swarm.health.verdicts import VERDICTS, VerdictStore
from scripts.swarm.store import MASTER, SwarmError

INTERVAL_ENV = "AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES"
QUIET_ENV = "AGENTIHOOKS_DOCTOR_QUIET_MINUTES"
INTERVAL_MINUTES = 10
QUIET_MINUTES = 120
JUDGED_REAL = ("established", "early-real")
FIXES = ("code", "tune")
FIX_PHASE = "p2"
SENDER = "doctor-timer"
IDLE = ("stopped", "stopping")


def verdicts(store, doctor):
    return VerdictStore(store.redis, store.key(doctor, "doctor-findings"))


def _details_key(store, doctor):
    return store.key(doctor, "doctor-details")


def _timer_key(store, doctor):
    return store.key(doctor, "doctor-timer")


def _minutes(environ, name, default):
    return int(environ.get(name) or default)


def interval_minutes(environ):
    return _minutes(environ, INTERVAL_ENV, INTERVAL_MINUTES)


def reset(store, doctor):
    store.redis.delete(_timer_key(store, doctor))


def record(store, doctor, found, now_ms, cooldown_ms):
    """Keeps every finding with its detail; returns the ones first seen or shown again after a verdict."""
    judgments = verdicts(store, doctor)
    before = {k: json.loads(v) for k, v in store.redis.hgetall(judgments.key).items()}
    shown = judgments.visible(found, now_ms, cooldown_ms, new_evidence=True)
    if found:
        details = {f.id: json.dumps({**f.as_dict(), "id": f.id, "measure": f.measure}) for f in found}
        store.redis.hset(_details_key(store, doctor), mapping=details)
    return [s for s in shown if s["id"] not in before or (s["verdict"] and not before[s["id"]]["returned"])]


def judged(store, doctor, finding_id):
    raw = store.redis.hget(_details_key(store, doctor), finding_id)
    stored = store.redis.hget(verdicts(store, doctor).key, finding_id)
    if raw is None or stored is None:
        raise SwarmError(f"no Doctor finding {finding_id}; the Doctor master's inbox lists each one with its id")
    return json.loads(raw), json.loads(stored)["verdict"]


def news(watched, found):
    lines = [f"New Doctor findings on the swarm {watched}:"]
    for f in found:
        again = f" It came back after a {f['verdict']['value']} verdict." if f["verdict"] else ""
        lines += [
            f"- {f['id']}: {f['summary']}.{again} Evidence: {'; '.join(f['evidence'][:3])}",
            f'  Judge it: agentihooks doctor {watched} verdict {f["id"]} {"|".join(VERDICTS)} --note "<why>"',
        ]
    lines.append(
        f"Turn an established or early-real one into tasks: agentihooks doctor {watched} task <finding> --fix code|tune."
    )
    return "\n".join(lines)


def run(store, doctor, now_ms, collect, close, environ=None):
    """One timer step: a detection pass on the last tick before the interval is up, then the close after quiet."""
    env = os.environ if environ is None else environ
    watched = store.peer(doctor)
    if not watched or store.config(doctor).state in IDLE:
        return []
    key, actions = _timer_key(store, doctor), []
    timer = {k: int(v) for k, v in store.redis.hgetall(key).items()}
    timer["gap"] = max(timer.get("gap", 0), now_ms - timer.get("tick", now_ms))
    interval = interval_minutes(env) * MINUTE_MS
    if "last" not in timer or now_ms - timer["last"] + timer["gap"] > interval:
        found, actions = collect(watched)
        new = record(store, doctor, found, now_ms, limits(env).cooldown_minutes * MINUTE_MS)
        timer = {"last": now_ms, "last_new": now_ms if new or "last_new" not in timer else timer["last_new"], "gap": 0}
        if new:
            InboxStore(store.redis).send(SENDER, seat_address(doctor, MASTER), news(watched, new))
            actions.append(f"doctor pass: {plural(len(new), 'new finding')} sent to the Doctor master")
    store.redis.hset(key, mapping={**timer, "tick": now_ms})
    quiet = _minutes(env, QUIET_ENV, QUIET_MINUTES)
    if now_ms - timer["last_new"] >= quiet * MINUTE_MS:
        close()
        actions.append(f"doctor closed after {plural(quiet, 'minute')} with no new finding")
    return actions


def fix_tasks(watched, finding, verdict, fix):
    """A troubleshoot task, then a code or tune task naming the number to move and the command that measures it."""
    if not verdict or verdict.get("value") not in JUDGED_REAL:
        value = verdict.get("value") if verdict else "no verdict"
        raise SwarmError(f"finding {finding['id']} has {value}; only established or early-real findings become tasks")
    if fix not in FIXES:
        raise SwarmError("a fix task is code or tune")
    stem = "fx-" + hashlib.sha1(finding["id"].encode()).hexdigest()[:8]
    kind, measure = finding["kind"], f"agentihooks doctor {watched} measure {finding['id']}"
    number = f"the {kind} measure on {finding['subject']}, {finding['measure']} when judged"
    cause = {
        "task": f"{stem}-cause",
        "title": f"Find the cause of the {kind} finding",
        "lane": "eng",
        "phase": FIX_PHASE,
        "kind": "troubleshoot",
        "description": f"Doctor finding {finding['id']}: {finding['summary']}. Threshold: {finding['threshold']}. "
        f"Evidence: {'; '.join(finding['evidence'])}. Show the root cause with evidence, then fix it or file it.",
        "contract": {
            "must": f"the root cause of {finding['summary']} is shown by evidence",
            "check": "the evidence in the task proof",
            "judge": "the Standards reader",
        },
    }
    fixed = {
        "task": f"{stem}-{fix}",
        "title": f"Move the number behind the {kind} finding",
        "lane": "eng",
        "phase": FIX_PHASE,
        "kind": fix,
        "depends_on": [cause["task"]],
        "description": f"Number to move: {number}. Threshold: {finding['threshold']}. Measure it before and after "
        f"with: {measure}. A code fix lands with a test first, then a new or tuned detector.",
        "contract": {
            "must": f"{number} falls, measured the same way before and after",
            "check": f"{measure}, run before and after the fix",
            "judge": "the Doctor master",
        },
    }
    return [cause, fixed]
