import json
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlencode

REPO = "the-cloud-clockwork/agentihooks"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
FAILURES = (subprocess.SubprocessError, ValueError, KeyError, RuntimeError)
SETTLE_READS = 4


class _Api:
    def __init__(self, run, max_calls, max_seconds):
        self.run = run
        self.max_calls = max_calls
        self.deadline = time.monotonic() + max_seconds
        self.calls = 0
        self.lock = threading.Lock()

    def get(self, endpoint: str) -> dict:
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if self.calls >= self.max_calls or remaining <= 0:
                raise RuntimeError("Actions API request or time budget exhausted")
            self.calls += 1
        response = self.run(
            ["gh", "api", endpoint], capture_output=True, text=True, check=True, timeout=min(25, remaining)
        )
        return json.loads(response.stdout)

    def pages(self, endpoint: str, field: str, settles: int) -> list[dict]:
        separator = "&" if "?" in endpoint else "?"
        for attempt in range(2 + settles):
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


def read(
    since_ms: int,
    now_ms: int,
    pending: list[int] | None = None,
    known: dict[str, dict] | None = None,
    *,
    run: Callable = subprocess.run,
    max_calls: int = 60,
    max_seconds: int = 25,
) -> dict:
    api = _Api(run, max_calls, max_seconds)
    root = f"repos/{REPO}/actions/runs"
    discovered, records = {}, []
    try:
        since = datetime.fromtimestamp(since_ms / 1000, timezone.utc).isoformat()
        until = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()
        queries = [{"created": f"{since}..{until}"}]
        queries.extend({"status": status} for status in ACTIVE)
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
                    failure = failure or exc
            if failure:
                raise failure
        return {"complete": True, "runs": records, "api_calls": api.calls, "discovered_runs": len(discovered)}
    except FAILURES as exc:
        return {
            "complete": False,
            "error": f"Actions API read failed: {type(exc).__name__}: {exc}",
            "runs": records,
            "api_calls": api.calls,
            "discovered_runs": len(discovered),
        }
