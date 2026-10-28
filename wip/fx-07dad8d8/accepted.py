"""Time observation_reconciliation._accepted for every exporter cursor on this host, largest transcript first."""
import json, os, time
from hooks.observability import agent_trace, trace_flush
from scripts.doctor import observation_reconciliation as rec, registry
rows = []
for path in agent_trace.CURSOR_DIR.glob("*.json"):
    cursor = registry.load(path)
    if not (cursor.get("paged") or {}).get("spans"):
        continue
    sid = path.stem
    req = registry.load(trace_flush.request_path(sid))
    harness = req.get("target") or "claude"
    transcript = trace_flush._transcript(sid, {"target": harness, **req})
    size = os.path.getsize(transcript) if transcript and os.path.exists(transcript) else 0
    rows.append((size, sid, harness, int((cursor.get("source") or {}).get("accepted_bytes", 0)), int(cursor["paged"]["spans"])))
rows.sort(reverse=True)
print("paged cursors", len(rows))
for size, sid, harness, accepted, spans in rows[:8]:
    t = time.perf_counter(); c = time.process_time()
    try:
        ids, agg = rec._accepted(sid, harness, lambda: False); n = len(ids); err = ""
    except Exception as e:
        n, agg, err = 0, 0, type(e).__name__
    print(json.dumps({"session": sid[:8], "harness": harness, "transcript_mb": round(size / 1e6, 1), "accepted_mb": round(accepted / 1e6, 1),
                      "paged_spans": spans, "ids": n, "wall_s": round(time.perf_counter() - t, 2), "cpu_s": round(time.process_time() - c, 2), "error": err}))
