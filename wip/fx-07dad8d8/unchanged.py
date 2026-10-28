"""Share of active bindings whose current trace did not change between scans GAP seconds apart."""
import json, os, sys, time
from dataclasses import asdict
from scripts.doctor import registry, traces_read as tr
from scripts.swarm.store import connect
slug, gap, rounds = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
get = tr.measured_client(os.environ)
def scan():
    seen = {}
    for b in registry.bindings([asdict(a) for a in connect().agents(slug)]):
        rows = get("traces", {"tags": [f"swarm:{slug}", f"agent:{b['agent']}"], "fields": "core", "orderBy": "timestamp.desc", "limit": 5, "page": 1})["data"]
        own = b["session_id"]
        cur = next((r for r in rows if r.get("sessionId") == own), None) if own else (rows or [None])[0]
        if cur:
            total = get("observations", {"traceId": cur["id"], "limit": 1, "page": 1})["meta"].get("totalItems")
            seen[b["agent"]] = (cur["id"], cur.get("updatedAt"), total)
    return seen
prev = scan()
for r in range(rounds):
    time.sleep(gap)
    now = scan()
    common = set(prev) & set(now)
    same = sorted(a for a in common if prev[a] == now[a])
    print(json.dumps({"round": r, "bindings": len(now), "compared": len(common), "unchanged": len(same), "unchanged_agents": same}), flush=True)
    prev = now
