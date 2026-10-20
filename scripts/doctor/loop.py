"""The Doctor master loop: a detection pass per interval, judged findings to tasks, and the close after quiet hours."""

import hashlib
import json
import os
from datetime import datetime, timezone

from scripts.doctor import ci_speed, ci_speed_read, github_status
from scripts.doctor.words import plural
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.health.findings import MINUTE_MS, Finding, limits
from scripts.swarm.health.verdicts import VERDICTS, VerdictStore
from scripts.swarm.store import MASTER, RedisStore, SwarmError

INTERVAL_ENV = "AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES"
QUIET_ENV = "AGENTIHOOKS_DOCTOR_QUIET_MINUTES"
INTERVAL_MINUTES = 10
QUIET_MINUTES = 120
SPEED_SLICE_MINUTES = 10
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


def retract(store, doctor, watched, cleared, now_ms):
    details = _details_key(store, doctor)
    gone = [finding_id for finding_id in cleared if store.redis.hexists(details, finding_id)]
    if not gone:
        return []
    store.redis.hdel(details, *gone)
    judgments = verdicts(store, doctor).key
    for finding_id, raw in zip(gone, store.redis.hmget(judgments, gone)):
        if raw:
            store.redis.hset(judgments, finding_id, json.dumps({**json.loads(raw), "retracted_at": now_ms}))
    text = "\n".join(
        [f"Retracted Doctor findings on the swarm {watched}: a corrected read of their jobs supersedes them."]
        + [f"- {finding_id}" for finding_id in gone]
    )
    mail = InboxStore(store.redis)
    for slug in (watched, doctor):
        store.redis.srem(store.key(doctor, "ci-speed-notified", slug), *gone)
        mail.send(SENDER, seat_address(slug, MASTER), text)
    return gone


def _incidents(state, now_ms):
    """The tracked GitHub incidents and the open Actions ones holding findings; an unread status page holds none."""
    current = github_status.unresolved()
    incidents = github_status.track(state.get("incidents", {}), current, now_ms)
    return incidents, [incident for incident in incidents.values() if current and incident["end"] is None]


def _crossings(state, runs, span, incidents, held):
    """This pass window's grouped CI speed findings, none while an incident holds them, and the superseded ids."""
    since, now_ms = span
    width = interval_minutes(os.environ) * MINUTE_MS
    window = now_ms - now_ms % width
    groups = {
        identity: entry for identity, entry in state.get("groups", {}).items() if entry["window"] >= window - width
    }
    cleared = ci_speed.cleared(runs)
    gone = [*ci_speed.prune(groups, cleared), *cleared]
    if not held:
        ci_speed.group(groups, runs, since, now_ms, window, github_status.windows(incidents, now_ms))
    state["groups"] = groups
    current = {identity: entry for identity, entry in groups.items() if entry["window"] == window}
    return ([] if held else ci_speed.grouped(current)), gone


def _hold(mail, watched, doctor, held):
    for incident in held:
        if incident["noticed"]:
            continue
        began = datetime.fromtimestamp(incident["start"] / 1000, timezone.utc).strftime("%d %B %H:%M UTC")
        text = (
            f"Doctor CI speed findings on the swarm {watched} are held: GitHub reports an unresolved incident "
            f"affecting Actions, {incident['name']}, since {began}. {incident['link']} "
            "Runs are still read and counted; findings resume when it resolves, and runs with jobs created "
            "during it are labelled."
        )
        for slug in (watched, doctor):
            mail.send(SENDER, seat_address(slug, MASTER), text)
        incident["noticed"] = True


def speed(store: RedisStore, doctor: str, watched: str, now_ms: int) -> list[str]:
    key = store.key(doctor, "ci-speed")
    state = json.loads(store.redis.get(key) or "{}")
    since = state.get("last", now_ms - MINUTE_MS)
    end = min(now_ms, since + SPEED_SLICE_MINUTES * MINUTE_MS)
    snapshot = ci_speed_read.read(
        since, end, pending=state.get("pending"), known=state.get("known"), active=end == now_ms
    )
    incidents, held = _incidents(state, now_ms)
    found, gone = _crossings(state, snapshot["runs"], (since, now_ms), incidents, held)
    report = {
        "complete": snapshot["complete"] and end == now_ms,
        "runs": len(snapshot["runs"]),
        "discovered_runs": snapshot["discovered_runs"],
        "jobs": sum(len(run["jobs"]) for run in snapshot["runs"]),
        "crossings": len(found),
        "api_calls": snapshot["api_calls"],
    }
    if not snapshot["complete"]:
        report["error"] = snapshot["error"]
    if not snapshot["complete"] and not held:
        found.append(
            Finding(
                "ci speed read",
                "agentihooks",
                "CI speed API read is incomplete",
                (snapshot["error"],),
                "Actions API read must complete",
                1,
            )
        )
    record(store, doctor, found, now_ms, limits(os.environ).cooldown_minutes * MINUTE_MS)
    report["retracted"] = len(retract(store, doctor, watched, gone, now_ms))
    report["unverified"] = snapshot.get("unverified", 0)
    mail = InboxStore(store.redis)
    _hold(mail, watched, doctor, held)
    for slug in (watched, doctor):
        notified = store.key(doctor, "ci-speed-notified", slug)
        fresh = [finding for finding in found if not store.redis.sismember(notified, finding.id)]
        if fresh:
            mail.send(
                SENDER,
                seat_address(slug, MASTER),
                news(watched, [{"id": finding.id, **finding.as_dict(), "verdict": None} for finding in fresh]),
            )
            store.redis.sadd(notified, *(finding.id for finding in fresh))
            store.redis.hset(_timer_key(store, doctor), "last_new", now_ms)
    if snapshot["complete"]:
        state.update(last=end, pending=[run["id"] for run in snapshot["runs"] if run["status"] != "completed"])
    finished = {
        str(run["id"]): {
            "status": run["status"],
            "run_attempt": run.get("run_attempt"),
            "jobs": [{field: job.get(field) for field in ci_speed.JOB_FIELDS} for job in run["jobs"]],
        }
        for run in snapshot["runs"]
        if run["status"] == "completed"
    }
    state["known"] = {} if snapshot["complete"] else {**state.get("known", {}), **finished}
    state.update(report=report, incidents=incidents)
    store.redis.set(key, json.dumps(state))
    status = (
        "complete"
        if report["complete"]
        else "catching up"
        if snapshot["complete"]
        else f"incomplete: {report['error']}"
    )
    paused = "".join(f"; findings held during GitHub incident {incident['name']}" for incident in held)
    return [
        f"doctor CI speed: {status}; {report['runs']} runs read of {report['discovered_runs']} discovered, "
        f"{report['jobs']} jobs, {report['crossings']} crossings, {report['api_calls']} API calls, "
        f"{report['retracted']} retracted, {report['unverified']} queued jobs not reread{paused}"
    ]


def run(store, doctor, now_ms, collect, close, environ=None, speed=None):
    """One timer step: a detection pass on the last tick before the interval is up, then the close after quiet."""
    env = os.environ if environ is None else environ
    watched = store.peer(doctor)
    if not watched or store.config(doctor).state in IDLE:
        return []
    key, actions = _timer_key(store, doctor), speed(watched) if speed else []
    timer = {k: int(v) for k, v in store.redis.hgetall(key).items()}
    timer["gap"] = max(timer.get("gap", 0), now_ms - timer.get("tick", now_ms))
    interval = interval_minutes(env) * MINUTE_MS
    if "last" not in timer or now_ms - timer["last"] + timer["gap"] > interval:
        found, collected = collect(watched)
        actions.extend(collected)
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
