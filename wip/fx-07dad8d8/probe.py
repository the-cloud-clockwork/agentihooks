"""Per binding Langfuse request count and latency for the Doctor active read, with the identity listing cost."""
import json, math, os, sys, threading, time
from collections import defaultdict
from dataclasses import asdict
from scripts.doctor import registry, traces_read as tr
from scripts.swarm.store import connect

slug = sys.argv[1]
get0 = tr.measured_client(os.environ)
stats = defaultdict(lambda: {"requests": 0, "seconds": 0.0, "max": 0.0, "kinds": defaultdict(int)})
local = threading.local()

def get(path, params):
    t = time.perf_counter()
    try:
        return get0(path, params)
    finally:
        d = time.perf_counter() - t
        s = stats[getattr(local, "who", "?")]
        s["requests"] += 1; s["seconds"] += d; s["max"] = max(s["max"], d)
        kind = path + ("/root" if params.get("type") == "AGENT" else "/fresh" if "fromStartTime" in params else "/page" if params.get("limit") == 100 else "/count" if params.get("limit") == 1 else "")
        s["kinds"][kind] += 1

agents = [asdict(a) for a in connect().agents(slug)]
active = registry.bindings(agents)
out = []
for b in active:
    local.who = b["agent"]
    t = time.perf_counter()
    rows = get("traces", {"tags": [f"swarm:{slug}", f"agent:{b['agent']}"], "fields": "core", "orderBy": "timestamp.desc", "limit": 5, "page": 1})["data"]
    found = [registry._remote_trace(r, registry._root(get, r["id"])) for r in rows]
    own = b["session_id"]
    cur = next((x for x in found if x["session_id"] == own), None) if own else (found or [None])[0]
    total = pages = 0
    t_core = t_list = 0.0
    if cur:
        registry._freshness(get, cur["id"], {}, 10)
        total = get("observations", {"traceId": cur["id"], "limit": 1, "page": 1})["meta"].get("totalItems") or 0
        t_core = time.perf_counter() - t
        t1 = time.perf_counter()
        from scripts.doctor.observation_reconciliation import _remote_ids
        try:
            _remote_ids(get, cur["id"], lambda: False)
        except Exception as e:
            print("listing fail", b["agent"], e, file=sys.stderr)
        t_list = time.perf_counter() - t1
        pages = math.ceil(total / 100)
    s = stats[b["agent"]]
    out.append({"agent": b["agent"], "harness": b["harness"], "observations": total, "id_pages": pages,
                "requests": s["requests"], "core_s": round(t_core, 2), "listing_s": round(t_list, 2),
                "total_s": round(time.perf_counter() - t, 2), "max_req_s": round(s["max"], 2),
                "kinds": dict(s["kinds"]), "trace": cur["id"] if cur else None, "fresh": None})
print(json.dumps(out, indent=0))
