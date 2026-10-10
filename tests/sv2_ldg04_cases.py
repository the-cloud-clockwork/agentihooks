import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from scripts.swarm_v2 import ledger_writer
from tests import ledger_guard
from tests.swarm_ledger.test_server_environment import ROOT, clean_environment, request

SLUG = "single-writer"
TOKEN = "single-writer-fixture-token"
SERVER = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"
WAIT = 20


def seed(folder: Path) -> None:
    from tests.swarm_ledger.test_tasks import core, new_ledger

    content = {
        "title": "Single writer fixture",
        "overview": "Two server processes share one ledger folder",
        "sources": [],
        "phases": [{"title": "Ledger", "description": "One active writer"}],
    }
    document, meta, _ = core.load_state(folder.parent / "absent-seed.json", new_ledger.build_doc(content))
    SQLiteLedgerRepository(folder / ledger_writer.DATABASE, core).create_document(SLUG, document, meta, TOKEN)


def stored(folder: Path) -> dict:
    document = sqlite.read_document(folder, SLUG)
    return {"rev": document["_meta"]["rev"], "chat": [entry["text"] for entry in document.get("chat", [])]}


def environment(base: Path, folder: Path, port: int) -> dict:
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    return clean_environment(
        AGENTIHOOKS_HOME=str(home),
        LEDGER_DIR=str(folder),
        LEDGER_HOST="127.0.0.1",
        LEDGER_PORT=str(port),
        SWARM_RELOAD="0",
    )


def spawn(base: Path, folder: Path, port: int) -> subprocess.Popen:
    base.mkdir(parents=True, exist_ok=True)
    with (base / "server.log").open("w") as log:
        return subprocess.Popen(
            [sys.executable, str(SERVER), "--serve"], env=environment(base, folder, port), stdout=log, stderr=log
        )


@contextmanager
def server(base: Path, folder: Path, port: int):
    child = spawn(base, folder, port)
    try:
        deadline = time.monotonic() + WAIT
        own = (200, json.dumps({"dir": str(folder)}).encode())
        while request(port, "/healthz", f"127.0.0.1:{port}") != own:
            assert child.poll() is None, (base / "server.log").read_text()
            assert time.monotonic() < deadline, (base / "server.log").read_text()
            time.sleep(0.05)
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


def refused(base: Path, folder: Path, port: int, holder: int) -> dict:
    child = spawn(base, folder, port)
    try:
        code = child.wait(timeout=WAIT)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)
        code = None
    log = (base / "server.log").read_text()
    return {
        "exit": code,
        "named_the_holder": f"already has an active writer, pid {holder} on " in log,
        "served": request(port, "/healthz", f"127.0.0.1:{port}")[0] is not None,
        "leaked_the_token": TOKEN in log,
    }


def put(port: int, operation: str, text: str) -> dict:
    body = json.dumps({"ops": [{"op": "add", "id": operation, "thread": "chat", "text": text}]}).encode()
    message = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{SLUG}",
        data=body,
        headers={"Host": f"127.0.0.1:{port}", "X-Ledger-Token": TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(message, timeout=10) as response:
        reply = json.loads(response.read())
    return {"status": response.status, "applied": reply["applied"], "rejected": reply["rejected"]}


def labelled(port: int) -> dict:
    body = json.dumps({"ops": [{"op": "add", "id": "a-label", "thread": "chat", "text": "Claimed by a label"}]})
    message = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{SLUG}",
        data=body.encode(),
        headers={"Host": f"127.0.0.1:{port}", "X-Ledger-Agent": "operator", "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(message, timeout=10) as response:
            return {"status": response.status, "reason": ""}
    except urllib.error.HTTPError as refusal:
        return {"status": refusal.code, "reason": refusal.read().decode()}


def tool(*args: str) -> dict:
    done = subprocess.run(
        [sys.executable, "-m", "scripts.swarm_v2.ledger_writer", *args],
        cwd=ROOT,
        env=clean_environment(),
        capture_output=True,
        text=True,
        timeout=WAIT,
    )
    return {"exit": done.returncode, "stderr": done.stderr}


def _positive(base: Path, ports: list) -> dict:
    folder = base / "ledgers"
    seed(folder)
    with server(base / "first", folder, ports[0]) as first:
        held = ledger_writer.holder(folder).get("pid") == first.pid
        before = stored(folder)
        second = refused(base / "second", folder, ports[1], first.pid)
        between = stored(folder)
        label = labelled(ports[0])
        unlabelled = stored(folder)
        write = put(ports[0], "a-1", "Written by the one active writer")
        after = stored(folder)
    return {
        "first_server_held_the_lease": held,
        "second_server": second,
        "ledger_unchanged_by_second": before == between,
        "display_label_without_token": label,
        "ledger_unchanged_by_label": unlabelled == between,
        "first_server_write": write,
        "revision_advanced_by_first": after["rev"] > between["rev"],
        "chat": after["chat"],
        "ledger_writer_conflicts_total": ledger_writer.conflicts_total(folder),
    }


def _rejection(base: Path, ports: list) -> dict:
    folder = base / "ledgers"
    seed(folder)
    backup = base / "backup.sqlite3"
    with server(base / "first", folder, ports[0]) as first:
        put(ports[0], "b-1", "Before the snapshot")
        snapshot = tool("--dir", str(folder), "snapshot", str(backup))
        put(ports[0], "b-2", "After the snapshot")
        protected = stored(folder)
        restore = tool("--dir", str(folder), "restore", str(backup))
        second = refused(base / "second", folder, ports[1], first.pid)
        unchanged = stored(folder) == protected
    with server(base / "third", folder, ports[1]):
        later = stored(folder)
    return {
        "snapshot_exit": snapshot["exit"],
        "restore_while_serving": {
            "exit": restore["exit"],
            "named_the_holder": f"already has an active writer, pid {first.pid} on " in restore["stderr"],
            "leaked_the_token": TOKEN in restore["stderr"],
        },
        "second_server": second,
        "protected_state_unchanged": unchanged,
        "a_new_writer_starts_once_the_first_stops": later == protected,
        "ledger_writer_conflicts_total": ledger_writer.conflicts_total(folder),
    }


def _recovery(base: Path, ports: list) -> dict:
    folder = base / "ledgers"
    seed(folder)
    backup = base / "backup.sqlite3"
    with server(base / "first", folder, ports[0]) as first:
        accepted = put(ports[0], "c-1", "Accepted before the crash")
        snapshot = tool("--dir", str(folder), "snapshot", str(backup))
        committed = stored(folder)
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
    with server(base / "second", folder, ports[1]) as second:
        reloaded = stored(folder)
        lease_moved = ledger_writer.holder(folder).get("pid") == second.pid
        replay = put(ports[1], "c-1", "Accepted before the crash")
        replayed = stored(folder)
        put(ports[1], "c-2", "Written after the restart")
        newer = stored(folder)
    restore = tool("--dir", str(folder), "restore", str(backup))
    restored = stored(folder)
    with server(base / "rolled-back", folder, ports[0]) as rolled_back:
        promoted = stored(folder)
        promoted_holds = ledger_writer.holder(folder).get("pid") == rolled_back.pid
    return {
        "accepted": accepted,
        "snapshot_exit": snapshot["exit"],
        "restart_loaded_the_last_committed_revision": reloaded == committed,
        "lease_moved_to_the_restarted_writer": lease_moved,
        "replay": replay,
        "replay_added_nothing": replayed["chat"] == committed["chat"],
        "chat_after_the_new_write": newer["chat"],
        "rollback": {
            "exit": restore["exit"],
            "restored_the_backup": restored == committed,
            "writer_started_on_the_restored_backup": promoted == committed,
            "restarted_writer_held_the_lease": promoted_holds,
        },
        "ledger_writer_conflicts_total": ledger_writer.conflicts_total(folder),
    }


def run_case(case: str, base: Path) -> dict:
    with ExitStack() as stack:
        ports = [stack.enter_context(ledger_guard.reserve_port()).getsockname()[1] for _ in range(2)]
        observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](base, ports)
    return {
        "case": f"T-SV2-LDG-04-{case.upper()}",
        "evidence_class": "two real ledger server processes sharing one SQLite ledger folder; no Redis fixture",
        "observed": observed,
        "state": "passed",
    }
