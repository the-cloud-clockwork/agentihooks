"""Panes the tick opened for a launch that failed: held two minutes so the failure stays on screen, then their last
lines are copied into the launch record and the pane is closed by its own id. Only a held pane id is ever read or
closed, so operator panes and pairing sessions outside the swarm are never touched."""

import json

CLOSE_AFTER_MS = 2 * 60 * 1000
TAIL_LINES = 20
HELD = "failed-panes"


def hold(store, slug, agent, now_ms):
    if not agent.pane_id:
        return
    entry = {"agent": agent.name, "task": agent.task, "failed_at": now_ms}
    store.redis.hset(store.key(slug, HELD), agent.pane_id, json.dumps(entry))


def held(store, slug):
    return {pane: json.loads(raw) for pane, raw in store.redis.hgetall(store.key(slug, HELD)).items()}


def lines(text, count):
    found = [line.rstrip() for line in text.splitlines()]
    while found and not found[-1]:
        found.pop()
    return found[-count:]


def _holder(store, slug, runtime, pane, entry):
    if holder := next((a.name for a in store.agents(slug) if a.pane_id == pane), ""):
        return f"{holder} holds it now"
    return f"{entry['agent']} is live" if entry["agent"] in runtime.live_names() else ""


def _tail(runtime, pane):
    try:
        return lines(runtime.pane_tail(pane, TAIL_LINES), TAIL_LINES)
    except Exception as exc:
        return [f"its lines could not be read: {exc}"]


def sweep(slug, store, runtime, now_ms):
    actions = []
    for pane, entry in held(store, slug).items():
        if now_ms - entry["failed_at"] < CLOSE_AFTER_MS:
            continue
        if reason := _holder(store, slug, runtime, pane, entry):
            store.redis.hdel(store.key(slug, HELD), pane)
            actions.append(f"kept pane {pane}: {reason}")
            continue
        store.note_launch_pane(slug, entry["agent"], _tail(runtime, pane), now_ms)
        runtime.close_pane(pane)
        store.redis.hdel(store.key(slug, HELD), pane)
        actions.append(f"closed pane {pane} of the failed launch of {entry['agent']} for {entry['task']}")
    return actions


def report(store, slug):
    return [row for row in store.launches(slug) if row.get("pane_lines")]


def status_lines(store, slug):
    out = []
    for row in report(store, slug):
        out.append(f"failed launch  {row['agent']}  {row['task']}  pane {row['pane_id']} closed: {row['error']}")
        out += [f"  | {line}" for line in row["pane_lines"]]
    return out
