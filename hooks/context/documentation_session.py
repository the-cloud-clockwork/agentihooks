import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

from hooks._async import fork_and_call
from hooks.common import inject_context, log
from hooks.config import AGENTIHOOKS_HOME as HOME
from hooks.context import documentation, documentation_evidence
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger import ledger_rank

TERMINAL = {"queued", "complete"}


WORKER_OPTIONS = {"timeout_sec": 180, "task_name": "documentation-session-end"}
NO_CHANGES = {"needs_documentation": False, "size": "none", "source": "no changes"}
UNKNOWN_TASK = {"needs_documentation": False, "size": "none", "source": "no ledger task"}
TASK_DEFAULTS = {
    "profile": ledger_rank.GENERATED_PROFILE,
    "lane": "eng",
    "kind": "code",
    "follow_up": True,
    "rank": "low",
    "not_duplicate": "Documents delivered behavior rather than implementing it",
}
DOCUMENTATION_INSTRUCTIONS = (
    "Extend the existing documentation and navigation with complete usage and configuration examples. "
    "Follow the project's documentation conventions; default to docs when no source exists."
)
NO_PULL_REQUEST = "none recorded on the source task, so no delivered diff is attached"
NOTICE_COMMAND = "Start it here: agentihooks init-agent --profile documentation"
NOTICE_SUBJECT_LENGTH = 160
TASK_HASH_LENGTH = 16
RETRY_LIMIT = 3
LOCK_SUFFIX = ".lock"
WRITE_SUFFIX = ".next"


def _path(session: str) -> Path:
    return HOME / "documentation" / (hashlib.sha256(session.encode()).hexdigest() + ".json")


def _write(path: Path, row: dict) -> None:
    target = path.with_suffix(WRITE_SUFFIX)
    target.write_text(json.dumps(row))
    target.replace(path)


def _show_notice(repository: Path) -> None:
    for path in sorted((HOME / "documentation").glob("*.json")):
        row = json.loads(path.read_text())
        if not row.get("notice") or row.get("shown") or row["repository"] != str(repository):
            continue
        with path.with_suffix(LOCK_SUFFIX).open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = json.loads(path.read_text())
            if row.get("notice") and not row.get("shown"):
                inject_context(row["notice"])
                row["shown"] = True
                _write(path, row)
                return


def start(payload: dict) -> None:
    if os.environ.get("AGENTIHOOKS_CLASSIFIER_CHILD") == "1" or not payload.get("session_id"):
        return
    try:
        path = _path(payload["session_id"])
        root = Path(documentation_evidence.git(Path(payload["cwd"]), "rev-parse", "--show-toplevel"))
        repository = Path(
            documentation_evidence.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).parent
        path.parent.mkdir(parents=True, exist_ok=True)
        _show_notice(repository)
        if path.exists():
            return
        _write(
            path,
            {
                "session": payload["session_id"],
                "root": str(root),
                "repository": str(repository),
                "base": documentation_evidence.git(root, "rev-parse", "HEAD"),
                "swarm": os.environ.get("AGENTIHOOKS_SWARM", ""),
                "task": os.environ.get("AGENTIHOOKS_SWARM_TASK", ""),
                "status": "active",
            },
        )
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
        log("documentation snapshot unavailable", {"error": type(exc).__name__})


def end(payload: dict) -> None:
    if os.environ.get("AGENTIHOOKS_CLASSIFIER_CHILD") == "1" or not payload.get("session_id"):
        return
    try:
        path = _path(payload["session_id"])
        if not path.exists():
            start(payload)
        if not path.exists():
            return
        with path.with_suffix(LOCK_SUFFIX).open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            row = json.loads(path.read_text())
            if row["status"] in TERMINAL:
                return
            if row["status"] == "active":
                row["status"] = "pending"
                _write(path, row)
        fork_and_call(process_pending, path, **WORKER_OPTIONS)
    except (OSError, ValueError) as exc:
        log("documentation dispatch unavailable", {"error": type(exc).__name__})


def _queue(ledger: LedgerClient, row: dict, source: dict, evidence: dict, verdict: dict) -> str:
    task = (
        "doc-"
        + hashlib.sha256(f"{row['swarm']}:{row['task'] or row['session']}".encode()).hexdigest()[:TASK_HASH_LENGTH]
    )
    if ledger.task(row["swarm"], task):
        return task
    pull_request = source.get("pr_url") or (NO_PULL_REQUEST if source else "")
    description = (
        f"Document the delivered behavior of {source.get('title', evidence['summary'])}.\n"
        f"Source task: {row['task']}. Delivered pull request: {pull_request}.\n"
        f"Change size: {verdict['size']}.\n"
        f"Source requirements: {source.get('description', '')}\n"
        f"Documentation sources: {json.dumps(evidence['doc_sources'])}\n"
        f"Documentation matches: {json.dumps(evidence['docs'])}\n"
        f"Delivered change evidence:\n{evidence['changes']}\n" + DOCUMENTATION_INSTRUCTIONS
    )
    ledger.add_task(
        row["swarm"],
        {
            "task": task,
            "title": "Document " + source.get("title", evidence["summary"]),
            "description": description,
            **TASK_DEFAULTS,
            "phase": source.get("phase", ""),
            "depends_on": [row["task"]] if source else [],
        },
        by="swarm",
    )
    return task


def _finish(path: Path, row: dict) -> None:
    ledger = LedgerClient(service=True) if row["swarm"] else None
    source = ledger.task(row["swarm"], row["task"]) or {} if ledger and row["task"] else {}
    if ledger and row["task"] and not source:
        row.update(status="complete", verdict=dict(UNKNOWN_TASK))
        _write(path, row)
        return
    root = Path(row["root"])
    if not root.is_dir():
        root = Path(row["repository"])
    evidence = documentation_evidence.collect(root, row["base"], source)
    verdict = row.get("verdict")
    if not source and not evidence["files"]:
        verdict = dict(NO_CHANGES)
    if not verdict or verdict["needs_documentation"] is None:
        verdict = documentation.classify(evidence)
    if source.get("profile") == "documentation" or (
        not source and not any(Path(p).suffix not in documentation_evidence.DOC_TYPES for p in evidence["files"])
    ):
        verdict["needs_documentation"] = False
    row.update(verdict=verdict)
    log("documentation verdict", {"session": row["session"], **verdict})
    if verdict["needs_documentation"] is None:
        _write(path, row)
        return
    row["status"] = "complete"
    if verdict["needs_documentation"] and ledger:
        row["documentation_task"] = _queue(ledger, row, source, evidence, verdict)
        row["status"] = "queued"
    elif verdict["needs_documentation"]:
        row["notice"] = f"Documentation needed for {evidence['summary'][:NOTICE_SUBJECT_LENGTH]}. " + NOTICE_COMMAND
    _write(path, row)


def process(path: Path) -> None:
    path = Path(path)
    with path.with_suffix(LOCK_SUFFIX).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        row = json.loads(path.read_text())
        if row["status"] != "pending":
            return
        path.touch()
        try:
            _finish(path, row)
        except Exception as exc:
            row.update(status="pending", error=type(exc).__name__)
            _write(path, row)
            log("documentation request retained", {"error": type(exc).__name__, "session": row["session"]})


def process_pending(path: Path) -> None:
    process(path)
    current = json.loads(path.read_text())
    retried = 0
    for pending in sorted(path.parent.glob("*.json"), key=lambda item: (item.stat().st_mtime_ns, item.name)):
        if pending == path:
            continue
        row = json.loads(pending.read_text())
        if row["swarm"] == current["swarm"] and row["status"] == "pending":
            process(pending)
            retried += 1
            if retried == RETRY_LIMIT:
                break
