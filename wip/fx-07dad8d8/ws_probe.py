"""Per binding cost of the Doctor active trace read, run on a scratch copy of the live reader state.

Usage: python ws_probe.py SLUG PASSES GAP_SECONDS OUT_DIR
Reads only: copies the live doctor-telemetry.json and doctor-traces cache into OUT_DIR and runs traces_read.record
there PASSES times, GAP_SECONDS apart, printing one JSON line per pass.
"""
import json, os, shutil, sys, threading, time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from scripts.doctor import observation_reconciliation as rec, registry, traces_read as tr
from scripts.swarm.runtime import SWARM_HOME
from scripts.swarm.store import connect

slug, passes, gap, out = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), Path(sys.argv[4])
live = Path(SWARM_HOME) / slug
home = out / "home"
(home / slug).mkdir(parents=True, exist_ok=True)
if (live / tr.STATE_FILE).exists():
    shutil.copy(live / tr.STATE_FILE, home / slug / tr.STATE_FILE)
if (live / tr.CACHE_DIR).exists() and not (home / slug / tr.CACHE_DIR).exists():
    shutil.copytree(live / tr.CACHE_DIR, home / slug / tr.CACHE_DIR)

here = threading.local()
per, lock, t0 = {}, threading.Lock(), [0.0]

def row(name):
    return per.setdefault(name, {"requests": 0, "langfuse_s": 0.0, "max_request_s": 0.0, "id_pages": 0,
                                 "accepted_wall_s": 0.0, "accepted_cpu_s": 0.0, "listing_reused": None,
                                 "start_s": None, "end_s": None, "errors": []})

base_get = tr.measured_client(os.environ)

def get(path, params):
    name = getattr(here, "binding", "history")
    started = time.perf_counter()
    try:
        return base_get(path, params)
    except Exception as exc:
        with lock:
            row(name)["errors"].append(f"{path} {type(exc).__name__}")
        raise
    finally:
        took = time.perf_counter() - started
        with lock:
            r = row(name)
            r["requests"] += 1
            r["langfuse_s"] += took
            r["max_request_s"] = max(r["max_request_s"], took)
            r["id_pages"] += params.get("limit") == rec.PAGE

attempts = registry._attempts

def timed_attempts(get_, slug_, binding, *rest):
    here.binding = binding["agent"]
    with lock:
        row(binding["agent"])["start_s"] = time.perf_counter() - t0[0]
    try:
        return attempts(get_, slug_, binding, *rest)
    finally:
        with lock:
            row(binding["agent"])["end_s"] = time.perf_counter() - t0[0]

accepted = rec._accepted

def timed_accepted(*args):
    wall, cpu = time.perf_counter(), time.thread_time()
    try:
        return accepted(*args)
    finally:
        r = row(getattr(here, "binding", "?"))
        r["accepted_wall_s"] += time.perf_counter() - wall
        r["accepted_cpu_s"] += time.thread_time() - cpu

cached_listing = registry._cached_listing

def watched_listing(prior, mark, total, listing):
    called = []
    ids = cached_listing(prior, mark, total, lambda: called.append(1) or listing())
    row(getattr(here, "binding", "?"))["listing_reused"] = not called
    return ids

registry._attempts, rec._accepted, registry._cached_listing = timed_attempts, timed_accepted, watched_listing

for n in range(passes):
    per.clear()
    agents = [asdict(a) for a in connect().agents(slug)]
    load = os.getloadavg()
    t0[0] = time.perf_counter()
    data = tr.record(slug, [], agents, int(time.time() * 1000), get, home=home)
    wall = time.perf_counter() - t0[0]
    active = {b["agent"]: b for b in data["active"]}
    bindings = []
    for name, r in per.items():
        if name == "history":
            continue
        b = active.get(name, {})
        bindings.append({"agent": name, "read": b.get("read"), "observations": (b.get("remote") or {}).get("observations"),
                         "local": bool(b.get("local")), **{k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()}})
    bindings.sort(key=lambda b: b["start_s"] or 0)
    unstarted = [b["agent"] for b in data["active"] if b["agent"] not in per]
    print(json.dumps({"pass": n, "at": datetime.now().isoformat(timespec="seconds"), "loadavg": load, "cpus": os.cpu_count(),
                      "workers": registry.READERS, "wall_s": round(wall, 2), "bindings": len(data["active"]),
                      "read": data["reader"]["active"]["read"], "failures": data["reader"]["failures"], "unstarted": unstarted,
                      "history": {k: round(v, 3) if isinstance(v, float) else v for k, v in per.get("history", {}).items()},
                      "per_binding": bindings}), flush=True)
    if n + 1 < passes:
        time.sleep(gap)
