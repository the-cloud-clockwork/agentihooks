"""One Doctor pass shape on rig-grade-swarm: registry.read with the real budget, concurrent with traces_read.history's listing and backfill, every Langfuse request logged."""
import json, os, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from scripts.doctor import registry, traces_read as tr
from scripts.swarm.store import connect

slug = sys.argv[1]; cache = Path(sys.argv[2]); state_path = Path(sys.argv[3])
get0 = tr.measured_client(os.environ)
log, t0 = [], time.perf_counter()
lock = threading.Lock()
def get(path, params):
    s = time.perf_counter()
    err = ""
    try:
        return get0(path, params)
    except Exception as e:
        err = type(e).__name__; raise
    finally:
        with lock:
            log.append({"thread": threading.current_thread().name, "path": path, "params": {k: v for k, v in params.items() if k != "tags"},
                        "tags": params.get("tags"), "start": round(s - t0, 3), "seconds": round(time.perf_counter() - s, 3), "error": err})
agents = [asdict(a) for a in connect().agents(slug)]
state = registry.load(state_path)
with ThreadPoolExecutor() as pool:
    started = time.perf_counter()
    reading = pool.submit(registry.read, slug, agents, get, state, 15.0, 10)
    traces, coverage, lost = tr.history(slug, get, cache, tr.Budget())
    history_s = time.perf_counter() - started
    active, failures = reading.result()
    active_s = time.perf_counter() - started
registry.save(state_path, state)
print(json.dumps({"bindings": len(active), "read": sum(1 for b in active if b["read"]), "failures": failures,
                  "history_s": round(history_s, 2), "active_s": round(active_s, 2), "coverage": {k: v for k, v in coverage.items() if k != "unreadable"},
                  "requests": log}))
