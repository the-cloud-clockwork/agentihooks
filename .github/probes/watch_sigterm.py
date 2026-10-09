import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path.cwd()
SCRIPTS = ROOT / "scripts" / "swarm_ledger"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "caps-2026-01-01"
WRAP = (
    "import faulthandler,signal,runpy,sys;"
    "faulthandler.register(signal.SIGUSR1, all_threads=True);"
    "sys.argv=sys.argv[1:];"
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)


def make_ledger(directory):
    os.environ["LEDGER_DIR"] = str(directory)
    core.LEDGER_DIR = directory
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "p", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


def proc_state(pid):
    found = {}
    for name in ("status", "wchan", "stack"):
        try:
            found[name] = Path(f"/proc/{pid}/{name}").read_text()
        except OSError as exc:
            found[name] = f"unreadable: {exc}"
    found["children"] = subprocess.run(
        ["ps", "--ppid", str(pid), "-o", "pid,stat,cmd"], capture_output=True, text=True
    ).stdout
    return found


def one(jitter):
    directory = Path(tempfile.mkdtemp(prefix="watchprobe-"))
    make_ledger(directory)
    beat = core.watch_path(SLUG, "watcher")
    env = {**os.environ, "LEDGER_DIR": str(directory), "LEDGER_PORT": "9"}
    out, err = open(directory / "child.out", "w+"), open(directory / "child.err", "w+")
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-c", WRAP, str(SCRIPTS / "watch_ledger.py"), SLUG, "--as", "watcher", "--interval", "0.1"],
        env=env,
        stdout=out,
        stderr=err,
    )
    for _ in range(50):
        if beat.exists():
            break
        time.sleep(0.1)
    ready = time.monotonic() - started
    if not beat.exists():
        proc.kill()
        proc.wait()
        return {"result": "no_beat", "ready": ready}
    delay = random.uniform(0, 0.3) if jitter else 0.0
    time.sleep(delay)
    proc.send_signal(signal.SIGTERM)
    sent = time.monotonic()
    try:
        code = proc.wait(timeout=5)
        latency = time.monotonic() - sent
        return {"result": "exit", "code": code, "latency": latency, "ready": ready, "delay": delay, "beat_left": beat.exists()}
    except subprocess.TimeoutExpired:
        state = proc_state(proc.pid)
        proc.send_signal(signal.SIGUSR1)
        time.sleep(2)
        proc.kill()
        proc.wait()
        out.seek(0)
        err.seek(0)
        return {
            "result": "timeout",
            "ready": ready,
            "delay": delay,
            "beat_left": beat.exists(),
            "proc": state,
            "stdout": out.read()[-4000:],
            "stderr": err.read()[-12000:],
        }


def main():
    runs, jitter = int(sys.argv[1]), sys.argv[2] == "jitter"
    results = [one(jitter) for _ in range(runs)]
    latencies = sorted(r["latency"] for r in results if r["result"] == "exit")
    summary = {
        "runs": runs,
        "jitter": jitter,
        "timeouts": sum(r["result"] == "timeout" for r in results),
        "no_beat": sum(r["result"] == "no_beat" for r in results),
        "nonzero_exit": sum(r["result"] == "exit" and r["code"] != 0 for r in results),
        "beat_left": sum(bool(r.get("beat_left")) for r in results),
        "latency_p50": latencies[len(latencies) // 2] if latencies else None,
        "latency_p99": latencies[int(len(latencies) * 0.99)] if latencies else None,
        "latency_max": latencies[-1] if latencies else None,
        "ready_max": max(r["ready"] for r in results),
    }
    print("SUMMARY " + json.dumps(summary), flush=True)
    for r in results:
        if r["result"] != "exit" or r["code"] != 0 or r["latency"] > 1:
            print("CASE " + json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
