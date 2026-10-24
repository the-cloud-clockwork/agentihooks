"""Dev promotion watch: the newest finished push run of Tests on dev files one urgent ci task per failed job and
raises a ledger alert; the failed jobs that lost their runner, and the failed jobs whose own error is the missing
output of a unit shard that lost its runner, share one runner outage task instead. The next green run whose promote
job succeeded closes those tasks and the alert. A run whose only failure is the Sonar job finding dev already moved
past its commit is reported as superseded and files nothing.

The minute tick calls it; CI runners cannot reach the ledger, so the fixer tasks are filed from the machine that can.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.ci_budget import GATE
from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, dev_red, naming, promotion_owner
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import PREFIX
from scripts.swarm_ledger import ledger_alerts, ledger_comments

if TYPE_CHECKING:
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import RedisStore, SwarmConfig

JOBS_JQ = ".jobs[] | {id, name, conclusion, check_run_url, steps} | @json"
ANNOTATIONS_JQ = ".[] | {annotation_level, message} | @json"
FAILED = ("failure", "timed_out")
SUPERSEDED_STEP = "Check dev still points at this commit"
SUPERSEDED = "Dev advanced before Sonar analysis; this unscanned revision cannot be promoted."
STEP_EXIT = "Process completed with exit code 1."
OUTAGE = "runner outage"
OUTAGE_SIGNS = (
    "Failed to resolve action download info",
    "Failed to download action",
    "Failed to download archive",
    "lost communication with the server",
    "The operation was canceled",
)
SETUP_STEP = "Set up job"
TEST_STEP = "Run tests"
SHARD_JOB = "unit"
SHARD_CONSUMERS = (
    "sonar",
    "shard-check",
    "coverage-ratchet",
    "coverage-baseline",
    "flake-budget",
)
MISSING_SHARD_SIGNS = (
    "Missing coverage for shard ",
    "No shard durations at ",
    "collected tests did not run in exactly one of ",
    "head shard coverage missing: ",
    "baseline shard coverage missing: ",
    "No flake reports under ",
)
REFRESH_JOB = "refresh-durations"
LOST_SAMPLES_SIGN = "kept no samples from durations-"
REFRESH_MS = 60_000
SOURCE = "promotion"
SENDER = "swarm"
TASK_PREFIX = "promo-"
UNNAMED = "Promotion red: a dev job"
NOT_DUPLICATE = (
    "The promotion watch files one task per failed dev job and run, and holds a job's cause while its task is open."
)
HASH = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,}\b")
NUMBER = re.compile(r"\d+(?:[.:]\d+)*[A-Za-z]*")
MASTER_ITEM = (
    "Dev push run {run_id} of Tests failed on new causes, so the swarm filed {tasks} in the {lane} lane and raised a "
    "promotion alert: {url}"
)
CLOSABLE = ("open", "blocked")
WORKING = ("claimed", "pr")
CLEARED = "A later dev push run of Tests passed, so the swarm closed this promotion task."
REPLACED = "Dev push run {run_id} of Tests failed on a new cause, so the swarm raised a new promotion alert."
PROMOTE_JOB = "promote"
HELD = "dev push run {run_id} passed but its promote job did not succeed, so the promotion red stays open"


def key(slug: str, part: str) -> str:
    return ":".join((PREFIX, slug, SOURCE, part))


def _read(repo_dir: str, endpoint: str, jq: str, run: Callable) -> list[dict]:
    output = run(
        ["gh", "api", endpoint, "--paginate", "--jq", jq],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [json.loads(line) for line in output.splitlines() if line]


def jobs(repo_dir: str, run_id: int, run: Callable = subprocess.run) -> list[dict]:
    return _read(repo_dir, f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100", JOBS_JQ, run)


def superseded(repo_dir: str, job: dict, run: Callable = subprocess.run) -> bool:
    steps = [s["name"] for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
    if steps != [SUPERSEDED_STEP]:
        return False
    found = _read(repo_dir, f"{job['check_run_url']}/annotations?per_page=100", ANNOTATIONS_JQ, run)
    messages = {a["message"] for a in found if a["annotation_level"] == "failure"}
    return SUPERSEDED in messages and messages <= {SUPERSEDED, STEP_EXIT}


def cause(repo_dir: str, job: dict, run: Callable = subprocess.run) -> str:
    detail = [s["name"] for s in job.get("steps") or [] if s.get("conclusion") in FAILED]
    if job.get("check_run_url"):
        found = _read(repo_dir, f"{job['check_run_url']}/annotations?per_page=100", ANNOTATIONS_JQ, run)
        detail += [
            a["message"] for a in found if a["annotation_level"] == "failure" and a["message"] not in ("", STEP_EXIT)
        ]
    return f"{job['name']}: {'; '.join(detail)}"


def runner_lost(job: dict, why: str) -> bool:
    steps = {s["name"]: s.get("conclusion") for s in job.get("steps") or []}
    return (
        any(sign in why for sign in OUTAGE_SIGNS)
        or steps.get(SETUP_STEP) in FAILED
        or (
            TEST_STEP in steps
            and steps[TEST_STEP] not in ("success", *FAILED)
            and not any(conclusion in FAILED for conclusion in steps.values())
        )
    )


def starves(failed: tuple[str, str], shards_lost: bool) -> bool:
    job = failed[0].split(" (")[0]
    if job == REFRESH_JOB:
        return LOST_SAMPLES_SIGN in failed[1]
    return shards_lost and job in SHARD_CONSUMERS and any(sign in failed[1] for sign in MISSING_SHARD_SIGNS)


def outages(causes: list[tuple[str, str]], named: dict[str, dict]) -> list[tuple[str, str]]:
    gone = [(job, why) for job, why in causes if runner_lost(named[job], why)]
    if not gone:
        return causes
    shards_lost = any(job.split(" (")[0] == SHARD_JOB for job, _ in gone)
    starved = [c for c in causes if c not in gone and starves(c, shards_lost)]
    signs = [sign for sign in OUTAGE_SIGNS if any(sign in why for _, why in gone)]
    because = "; ".join(signs) or f"{SETUP_STEP} failed or {TEST_STEP} never ran"
    names = ", ".join(job for job, _ in gone)
    why = f"{OUTAGE}: the jobs {names} lost their runner infrastructure ({because})"
    if starved:
        why += f"; the jobs {', '.join(job for job, _ in starved)} failed for want of their unit shard output"
    return [(OUTAGE, why), *[cause for cause in causes if cause not in gone and cause not in starved]]


def failing_tests(repo: str, job: dict, run: Callable) -> set[str]:
    if job["name"].split(" (")[0] != SHARD_JOB or not job.get("id"):
        return set()
    if not any(s["name"] == TEST_STEP and s.get("conclusion") in FAILED for s in job.get("steps") or []):
        return set()
    log = run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return set(re.findall(r"\b(?:FAILED|ERROR) (tests/.+?)(?: - |(?:\s*$))", log, re.MULTILINE))


def test_causes(
    causes: list[tuple[str, str]], named: dict[str, dict], repo: str, run: Callable
) -> list[tuple[str, str]]:
    groups: dict[str, list[str]] = {}
    remaining = []
    for job, why in causes:
        tests = failing_tests(repo, named[job], run) if job in named else set()
        if not tests or any(
            s["name"] != TEST_STEP and s.get("conclusion") in FAILED for s in named[job].get("steps") or []
        ):
            remaining.append((job, why))
        for test in sorted(tests):
            groups.setdefault(test, []).append(job)
    jobs = [job for jobs in groups.values() for job in jobs]
    consequences = [(job, why) for job, why in remaining if coverage_consequence(job, why, jobs)]
    grouped = []
    for test, jobs in groups.items():
        identity = f"unit test {test}"
        detail = f"{identity}: failed in {', '.join(jobs)}"
        if consequences:
            detail += "; consequences: " + "; ".join(why for _, why in consequences)
        grouped.append((identity, detail))
    return [*grouped, *[cause for cause in remaining if cause not in consequences]]


def coverage_consequence(job: str, why: str, jobs: list[str]) -> bool:
    if job.split(" (")[0] != "sonar":
        return False
    missing = set(re.findall(r"Missing coverage for shard (\d+)", why))
    shards = {match[1] for name in jobs if (match := re.fullmatch(r"unit \(3\.12, (\d+)\)", name))}
    allowed = {"Wait for the coverage merge", "Hold the Delivery L2 conditions"}
    details = why.removeprefix(f"{job}: ").split("; ")
    return (
        bool(missing)
        and missing <= shards
        and all(
            part in allowed
            or re.fullmatch(r"(?:Missing coverage for shard \d+|Process completed with exit code \d+\.)", part)
            for part in details
        )
    )


def normal(job: str, why: str) -> str:
    if job.startswith("unit test "):
        return job
    return job + NUMBER.sub("#", HASH.sub("#", why.removeprefix(job)))


def failed_jobs(found: list[dict]) -> list[str]:
    failed = sorted({j["name"] for j in found if j["conclusion"] in FAILED})
    return [name for name in failed if name != GATE] or failed


def promoted(found: list[dict]) -> bool:
    runs = [j["conclusion"] for j in found if j["name"].split(" / ")[0] == PROMOTE_JOB]
    return bool(runs) and all(conclusion == "success" for conclusion in runs)


def title(job: str) -> str:
    named = "Promotion red: " + " ".join(re.sub(r"[—–_/]", " ", job).split())
    return UNNAMED if ledger_comments.problems(named, "item") else named


def _promotion_tasks(doc: dict) -> list[dict]:
    return [t for t in doc.get("tasks", []) if t["id"].startswith(TASK_PREFIX)]


def _live_alerts(doc: dict) -> list[dict]:
    return [a for a in doc.get("alerts", []) if a.get("source") == SOURCE and a.get("state") != "done"]


def _held_causes(redis, slug: str, doc: dict) -> set[str]:
    filed = redis.hgetall(key(slug, "jobs"))
    live = {t["id"] for t in _promotion_tasks(doc) if t.get("state") != "done"}
    if gone := [task_id for task_id in filed if task_id not in live]:
        redis.hdel(key(slug, "jobs"), *gone)
    return {why for task_id, why in filed.items() if task_id in live}


def _red(slug, store, ledger, doc, last, causes, repo):
    redis, run_id, sha, url = store.redis, last["id"], last["head_sha"], last["html_url"]
    held, present = _held_causes(redis, slug, doc), {t["id"] for t in doc.get("tasks", [])}
    for previous in promotion_owner.history(repo, redis) - {slug}:
        held.update(_held_causes(redis, previous, ledger.state(previous)))
    lane, actions, new = "", [], []
    for n, (job, why) in enumerate(causes):
        task_id = f"{TASK_PREFIX}{run_id}-{n}"
        if normal(job, why) in held or job in held or task_id in present:
            continue
        detail = why.removeprefix(job).removeprefix(": ").removesuffix(".")
        where = "lost its runner infrastructure" if job == OUTAGE else f"failed in the job {job}"
        description = (
            f"The dev push run {run_id} of Tests {where} at commit {sha}: {url}. "
            + (f"Its cause: {detail}. " if detail else "")
            + ("No code change is due" if job == OUTAGE else "Fix it on dev")
            + "; the swarm closes this task when a later dev push run passes."
        )
        lane = lane or _lane(store, slug)
        fields = {"task": task_id, "title": title(job), "lane": lane, "kind": "ci", "rank": "urgent"}
        redis.hset(key(slug, "jobs"), task_id, OUTAGE if job == OUTAGE else normal(job, why))
        ledger.add_task(slug, {**fields, "description": description, "not_duplicate": NOT_DUPLICATE}, SENDER)
        actions.append(f"filed {task_id} for the failed dev job {job}")
        new.append((task_id, why))
    if new or not _live_alerts(doc):
        named = [why.removesuffix(": ").removesuffix(".") for _, why in new] or [job for job, _ in causes]
        actions += _alert(slug, ledger, doc, last, named)
    if new:
        actions += _tell_master(slug, store, last, [task_id for task_id, _ in new], lane)
    return actions


def _alert(slug, ledger, doc, last, named):
    run_id = last["id"]
    for alert in _live_alerts(doc):
        ledger.close_alert(slug, alert["id"], REPLACED.format(run_id=run_id))
    head = f"Dev promotion is red: push run {run_id} at commit {last['head_sha']} failed"
    tail = f". {last['html_url']}"
    where = f" in {', '.join(named)}" if named else ""
    ledger.raise_alert(slug, SOURCE, head + where[: ledger_alerts.MAX_OUTCOME - len(head) - len(tail)] + tail)
    return [f"raised the promotion alert for dev push run {run_id}"]


def _tell_master(slug, store, last, new, lane):
    mail = Mail(InboxStore(store.redis), store, slug)
    text = MASTER_ITEM.format(run_id=last["id"], tasks=", ".join(new), lane=lane, url=last["html_url"])
    if mail.send(f"promotion-{last['id']}", mail.master, text, ref=new[0]):
        return [f"told the master about the new promotion causes of dev push run {last['id']}"]
    return []


def _lane(store, slug: str) -> str:
    seats = capacity.read(store, slug).get("effective", {}).get("ci")
    return "eng" if seats == 0 else "ci"


def _green(slug, ledger, doc, last):
    run_id, actions = last["id"], []
    for task in [t for t in _promotion_tasks(doc) if t.get("state") in CLOSABLE]:
        live = ledger.update_task(slug, task["id"], {"state": "done"}, by=SENDER, if_state=CLOSABLE)
        if live.get("state") == "done":
            ledger.comment(slug, task["id"], CLEARED, by=SENDER)
            actions.append(f"closed {task['id']}, dev push run {run_id} passed")
    for alert in _live_alerts(doc):
        ledger.close_alert(slug, alert["id"], f"Dev push run {run_id} of Tests passed.")
        actions.append(f"cleared the promotion alert, dev push run {run_id} passed")
    return actions


def _recover(slug, redis, ledger, doc, last, repo):
    actions = _green(slug, ledger, doc, last)
    _release(redis, slug, doc)
    for previous in promotion_owner.history(repo, redis) - {slug}:
        state = ledger.state(previous)
        actions.extend(_green(previous, ledger, state, last))
        _release(redis, previous, state)
    return actions


def _release(redis, slug, doc):
    working = {t["id"] for t in _promotion_tasks(doc) if t.get("state") in WORKING}
    if gone := [task_id for task_id in redis.hkeys(key(slug, "jobs")) if task_id not in working]:
        redis.hdel(key(slug, "jobs"), *gone)


def _refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    doc: dict,
    now_ms: int,
    run: Callable = subprocess.run,
) -> list[str]:
    redis = store.redis
    if naming.PROOF_RE.fullmatch(slug) or now_ms - int(redis.get(key(slug, "checked")) or 0) < REFRESH_MS:
        return []
    redis.set(key(slug, "checked"), now_ms)
    try:
        last = dev_red.latest(config.repo, run)
        if last is None or int(redis.get(key(slug, "seen")) or 0) >= last["id"]:
            return []
        found = jobs(config.repo, last["id"], run)
        failed = failed_jobs(found)
        stale = bool(failed) and all(superseded(config.repo, j, run) for j in found if j["name"] in failed)
        named = {j["name"]: j for j in found}
        causes = [] if stale else outages([(job, cause(config.repo, named[job], run)) for job in failed], named)
        causes = test_causes(causes, named, config.repo, run)
    except dev_red.READ_ERRORS as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        print(f"promotion watch skipped, reading dev push runs failed: {error}", file=sys.stderr)
        return []
    if stale:
        actions = [f"dev push run {last['id']} was superseded before Sonar analysis"]
    elif last["conclusion"] == "failure":
        actions = _red(slug, store, ledger, doc, last, causes, config.repo)
    elif not promoted(found):
        actions = [HELD.format(run_id=last["id"])]
    else:
        actions = _recover(slug, redis, ledger, doc, last, config.repo)
    redis.set(key(slug, "seen"), last["id"])
    return actions


def refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    doc: dict,
    now_ms: int,
    run: Callable = subprocess.run,
) -> list[str]:
    if naming.PROOF_RE.fullmatch(slug):
        return []
    with promotion_owner.intake(slug, config.repo, store) as owner:
        if owner != slug:
            return []
        record = promotion_owner.key(config.repo)
        seen = max(int(store.redis.hget(record, "seen") or 0), int(store.redis.get(key(slug, "seen")) or 0))
        if seen:
            store.redis.set(key(slug, "seen"), seen)
        if slug in store.slugs():
            doc = ledger.state(slug)
        actions = _refresh(slug, config, store, ledger, doc, now_ms, run)
        if handled := store.redis.get(key(slug, "seen")):
            store.redis.hset(record, "seen", handled)
        return actions
