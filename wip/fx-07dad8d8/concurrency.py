"""Langfuse throughput against concurrent readers, on the active read's own request kinds."""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from scripts.doctor import registry, traces_read as tr
from scripts.swarm.store import connect
slug = sys.argv[1]
get = tr.measured_client(os.environ)
active = registry.bindings([asdict(a) for a in connect().agents(slug)])
calls = []
for b in active:
    calls.append(("traces", {"tags": [f"swarm:{slug}", f"agent:{b['agent']}"], "fields": "core", "orderBy": "timestamp.desc", "limit": 5, "page": 1}))
traces = [r["id"] for p, q in calls for r in get(p, q)["data"][:1]]
for t in traces:
    calls.append(("observations", {"traceId": t, "limit": 1, "page": 1}))
    calls.append(("observations", {"traceId": t, "limit": 100, "page": 1}))
for workers in (1, 3, 6, 9, 12):
    took = []
    def one(c):
        s = time.perf_counter(); get(*c); took.append(time.perf_counter() - s)
    s = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, calls))
    wall = time.perf_counter() - s
    took.sort()
    print(f"workers {workers}: {len(calls)} requests wall {wall:.2f} s, {len(calls)/wall:.1f} per s, p50 {took[len(took)//2]:.2f} p90 {took[int(len(took)*.9)]:.2f} max {took[-1]:.2f}", flush=True)
