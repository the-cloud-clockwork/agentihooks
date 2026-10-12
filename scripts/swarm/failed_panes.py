"""Only a pane id held here is ever read or closed, and only while herdr still shows the terminal its launch opened,
so operator panes and pairing sessions outside the swarm are never touched."""

import json

CLOSE_AFTER_MS = 2 * 60 * 1000
TAIL_LINES = 20
HELD = "failed-panes"


def hold(store, slug, agent, now_ms, reason=""):
    if not agent.pane_id:
        return
    entry = {"agent": agent.name, "task": agent.task, "failed_at": now_ms, "reason": reason}
    store.redis.hset(store.key(slug, HELD), agent.pane_id, json.dumps(entry))


def held(store, slug):
    return {pane: json.loads(raw) for pane, raw in store.redis.hgetall(store.key(slug, HELD)).items()}


def lines(text, count):
    found = [line.rstrip() for line in text.splitlines()]
    while found and not found[-1]:
        found.pop()
    return found[-count:]


def _tail(runtime, pane):
    try:
        return lines(runtime.pane_tail(pane, TAIL_LINES), TAIL_LINES)
    except Exception as exc:
        return [f"its lines could not be read: {exc}"]


def _close(slug, store, runtime, pane, entry):
    key, agent = store.key(slug, HELD), entry["agent"]
    if holders := [a.name for a in store.agents(slug) if a.pane_id == pane]:
        store.redis.hdel(key, pane)
        return f"kept pane {pane}: {holders[0]} holds it now"
    if agent in runtime.live_names():
        return ""
    try:
        launched = runtime.launched_pane(pane, agent)
    except Exception as exc:
        return f"could not read pane {pane} of the failed launch of {agent}, retrying next tick: {exc}"
    if not launched:
        store.redis.hdel(key, pane)
        return f"dropped pane {pane}: herdr no longer shows the failed launch of {agent} there"
    store.note_launch_pane(slug, agent, _tail(runtime, pane), entry["reason"])
    try:
        runtime.close_pane(pane)
    except Exception as exc:
        return f"could not close pane {pane} of the failed launch of {agent}, retrying next tick: {exc}"
    store.redis.hdel(key, pane)
    return f"closed pane {pane} of the failed launch of {agent} for {entry['task']}"


def sweep(slug, store, runtime, now_ms):
    actions = []
    for pane, entry in held(store, slug).items():
        if now_ms - entry["failed_at"] >= CLOSE_AFTER_MS and (said := _close(slug, store, runtime, pane, entry)):
            actions.append(said)
    return actions


def report(store, slug):
    return [row for row in store.launches(slug) if row.get("pane_lines")]


def status_lines(store, slug):
    out = []
    for row in report(store, slug):
        out.append(f"failed launch  {row['agent']}  {row['task']}  pane {row['pane_id']} closed: {row['error']}")
        out += [f"  | {line}" for line in row["pane_lines"]]
    return out
