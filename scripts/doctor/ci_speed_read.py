import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from scripts.doctor import ci_speed
from scripts.swarm import github_reads
from scripts.swarm.ledger_events import iso_ms

REPO = "the-cloud-clockwork/agentihooks"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
FAILURES = (subprocess.SubprocessError, ValueError, KeyError, RuntimeError)
SETTLE_READS = 4
REREADS = 20


@dataclass
class Progress:
    pending: list[int] | None
    responses: dict[str, dict]


class _BudgetExhausted(RuntimeError):
    pass


class _Api:
    def __init__(self, run, max_calls, max_seconds, responses=None):
        self.run = run
        self.max_calls = max_calls
        self.deadline = time.monotonic() + max_seconds
        self.calls = 0
        self.lock = threading.Lock()
        self.responses = responses

    def get(self, endpoint: str) -> dict:
        with self.lock:
            if self.responses is not None and endpoint in self.responses:
                return self.responses[endpoint]
        body = self.request(endpoint)
        if self.responses is not None:
            self.responses[endpoint] = body
        return body

    def request(self, endpoint: str) -> dict:
        for attempt in range(2):
            with self.lock:
                remaining = self.deadline - time.monotonic()
                if self.calls >= self.max_calls or remaining <= 0:
                    raise _BudgetExhausted("Actions API request or time budget exhausted")
                self.calls += 1
            try:
                response = github_reads.rest(
                    "doctor.ci_speed",
                    ["api", endpoint],
                    self.run,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=min(25, remaining),
                )
            except subprocess.CalledProcessError as exc:
                status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", exc.stderr or "")
                if not status:
                    raise
                code = status[1]
                if attempt == 0 and code in ("502", "503", "504"):
                    continue
                raise RuntimeError(f"Actions API request failed: HTTP {code}; endpoint={endpoint}") from None
            return json.loads(response.stdout)

    def pages(self, endpoint: str, field: str, settles: int) -> list[dict]:
        separator = "&" if "?" in endpoint else "?"
        for attempt in range(2 + settles):
            if attempt and self.responses is not None:
                for key in list(self.responses):
                    if key.startswith(f"{endpoint}{separator}per_page="):
                        del self.responses[key]
            rows, totals = {}, set()
            page = 1
            while True:
                body = self.get(f"{endpoint}{separator}per_page=100&page={page}")
                total = body["total_count"]
                totals.add(total)
                rows.update((row["id"], row) for row in body[field])
                if len(rows) >= total:
                    return list(rows.values())
                if len(body[field]) < 100:
                    if len(totals) > 1:
                        return list(rows.values())
                    break
                page += 1
            if 1 <= attempt <= settles:
                if page > 1 or self.deadline - time.monotonic() <= 2:
                    break
                time.sleep(1)
        raise RuntimeError(
            "Actions API pagination ended before the full count: "
            f"endpoint={endpoint}; page={page}; rows={len(rows)}; total={total}"
        )


def _final(kept: dict, workflow: dict) -> bool:
    finished = kept.get("status") == workflow.get("status") == "completed"
    return finished and kept.get("run_attempt") == workflow.get("run_attempt")


def _reread(api, pool, records, now_ms):
    stale = [
        (run, n)
        for run in records
        for n, job in enumerate(run["jobs"])
        if job.get("status") in ACTIVE
        and not ci_speed.started(job)
        and now_ms - iso_ms(job["created_at"]) > ci_speed.SHORT_WAIT_MS
    ]
    reads = {
        pool.submit(api.get, f"repos/{REPO}/actions/jobs/{run['jobs'][n]['id']}"): (run, n)
        for run, n in stale[:REREADS]
    }
    unverified = len(stale) - len(reads)
    for future in as_completed(reads):
        run, n = reads[future]
        try:
            fresh = future.result()
        except FAILURES:
            unverified += 1
            continue
        run["jobs"] = [*run["jobs"][:n], fresh, *run["jobs"][n + 1 :]]
    return unverified


def read(
    since_ms: int,
    now_ms: int,
    pending: list[int] | Progress | None = None,
    known: dict[str, dict] | None = None,
    *,
    run: Callable = subprocess.run,
    budget: tuple[int, int] = (60, 25),
    active: bool = True,
) -> dict:
    progress = pending if isinstance(pending, Progress) else None
    api = _Api(run, *budget, responses=progress.responses if progress else None)
    pending = progress.pending if progress else pending
    root = f"repos/{REPO}/actions/runs"
    discovered, records = {}, []
    try:
        since = datetime.fromtimestamp(since_ms / 1000, timezone.utc).isoformat()
        until = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()
        queries = [{"created": f"{since}..{until}"}]
        queries.extend({"status": status} for status in ACTIVE if active)
        for query in queries:
            for workflow in api.pages(
                f"{root}?{urlencode(query)}", "workflow_runs", SETTLE_READS if "status" in query else 0
            ):
                discovered[workflow["id"]] = workflow
        with ThreadPoolExecutor(max_workers=6) as pool:
            previous = [
                pool.submit(api.get, f"{root}/{identity}") for identity in pending or [] if identity not in discovered
            ]
            for future in as_completed(previous):
                workflow = future.result()
                discovered[workflow["id"]] = workflow
            jobs, failure = {}, None
            for identity, workflow in discovered.items():
                kept = (known or {}).get(str(identity))
                if kept and _final(kept, workflow):
                    records.append({**workflow, "jobs": kept["jobs"]})
                else:
                    jobs[pool.submit(api.pages, f"{root}/{identity}/jobs?filter=latest", "jobs", 0)] = workflow
            for future in as_completed(jobs):
                try:
                    records.append({**jobs[future], "jobs": future.result()})
                except FAILURES as exc:
                    if failure is None or isinstance(failure, _BudgetExhausted):
                        failure = exc
            if failure:
                raise failure
            unverified = _reread(api, pool, records, now_ms)
        return {
            "complete": True,
            "runs": records,
            "api_calls": api.calls,
            "discovered_runs": len(discovered),
            "unverified": unverified,
        }
    except FAILURES as exc:
        error_type = RuntimeError if isinstance(exc, _BudgetExhausted) else type(exc)
        return {
            "complete": False,
            "error": f"Actions API read failed: {error_type.__name__}: {exc}",
            **({"paused": True} if progress and isinstance(exc, _BudgetExhausted) else {}),
            "runs": records,
            "api_calls": api.calls,
            "discovered_runs": len(discovered),
        }


def attempt(identity: int, number: int, *, run: Callable = subprocess.run, budget: tuple[int, int] = (20, 60)) -> dict:
    api = _Api(run, *budget)
    root = f"repos/{REPO}/actions/runs/{identity}/attempts/{number}"
    return {**api.get(root), "jobs": api.pages(f"{root}/jobs", "jobs", 0)}
