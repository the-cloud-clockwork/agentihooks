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

    def pages(self, endpoint: str, field: str) -> list[dict]:
        rows = []
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            body = self.get(f"{endpoint}{separator}per_page=100&page={page}")
            rows.extend(body[field])
            if len(rows) >= body["total_count"]:
                return rows
            if not body[field]:
                raise RuntimeError("Actions API pagination ended before the full count")
            page += 1


def read(
    since_ms: int,
    now_ms: int,
    pending: list[int] | None = None,
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
            for workflow in api.pages(f"{root}?{urlencode(query)}", "workflow_runs"):
                discovered[workflow["id"]] = workflow
        with ThreadPoolExecutor(max_workers=6) as pool:
            previous = [
                pool.submit(api.get, f"{root}/{identity}") for identity in pending or [] if identity not in discovered
            ]
            for future in as_completed(previous):
                workflow = future.result()
                discovered[workflow["id"]] = workflow
            jobs = {
                pool.submit(api.pages, f"{root}/{identity}/jobs?filter=latest", "jobs"): workflow
                for identity, workflow in discovered.items()
            }
            for future in as_completed(jobs):
                records.append({**jobs[future], "jobs": future.result()})
        return {"complete": True, "runs": records, "api_calls": api.calls, "discovered_runs": len(discovered)}
    except (subprocess.SubprocessError, ValueError, KeyError, RuntimeError) as exc:
        return {
            "complete": False,
            "error": f"Actions API read failed: {type(exc).__name__}: {exc}",
            "runs": records,
            "api_calls": api.calls,
            "discovered_runs": len(discovered),
        }
