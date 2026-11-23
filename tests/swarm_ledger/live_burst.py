"""A production size ledger and a burst of live changes replayed on the page at 1920 by 1080.

The ledger is the recorded 9 November corpus (tasks, phases, plans, slices and freezes with their real links), with
text and comments added until the snapshot reaches production size, trimmed for the page the way the server trims it.
Each change is the server's own patch between two states: a comment on a live task, a state change or a rank change.
The replay applies every patch the way the event stream does and records the long tasks, the longest task, the render
time per change, the item rows each change rebuilt and how long opening a closed section takes.

Run `python -m tests.swarm_ledger.live_burst` from a checkout to measure that checkout's page.
"""

import argparse
import copy
import gzip
import json
import random
import time
from pathlib import Path
from typing import Any

from scripts.swarm_ledger import ledger_closed
from scripts.swarm_ledger.events.patch import diff
from tests.swarm_ledger.ledger_page import served_ledger, shell_html, show

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ledger-2026-11-09.json.gz"
WORDS = "agent claim review merge queue shard runner hive tick ledger phase slice freeze focus proof gate lab".split()
OPEN = ("phases-box", "tasks-box", "questions-box")
NEXT_STATE = {"open": "claimed", "claimed": "pr", "pr": "open", "blocked": "open"}
DAYS_MS = 3 * 86_400_000

REPLAY = """async ({initial, patches, gap, open}) => {
  const main = document.querySelector('script[type=module]').src;
  const sync = await import(new URL('sync.js', main));
  const {applyPatch} = await import(new URL('patch.js', main));
  for (const id of open) document.getElementById(id).open = true;
  document.getElementById('followups-box').open = false;
  let state = JSON.parse(initial);
  sync.receiveLedger(state);
  await new Promise((done) => setTimeout(done, 4000));
  const tasks = [];
  const observer = new PerformanceObserver((list) => { for (const e of list.getEntries()) tasks.push(e.duration); });
  observer.observe({type: 'longtask'});
  let last = 0, added = 0;
  const watch = new MutationObserver((records) => {
    last = performance.now();
    for (const r of records) for (const n of r.addedNodes) if (n.nodeType === 1 && /^item-/.test(n.id || '')) added++;
  });
  watch.observe(document.getElementById('main-content'), {childList: true, subtree: true, characterData: true, attributes: true});
  const perChange = [];
  for (const patch of patches) {
    const start = performance.now();
    last = start;
    added = 0;
    state = applyPatch(state, patch);
    sync.receiveLedger(state);
    await new Promise((done) => setTimeout(done, gap));
    perChange.push({render: Math.round(last - start), rows: added});
  }
  await new Promise((done) => setTimeout(done, 3000));
  observer.disconnect();
  watch.disconnect();
  const openStart = performance.now();
  document.getElementById('followups-box').open = true;
  await new Promise((done) => { const check = () => document.querySelector('#followups > li') ? done() : setTimeout(check, 1); check(); });
  return {longTasks: tasks.length, longest: Math.round(Math.max(0, ...tasks)), blocked: Math.round(tasks.reduce((a, b) => a + b, 0)),
          perChange, openMs: Math.round(performance.now() - openStart), elements: document.getElementsByTagName('*').length,
          heapMB: Math.round((performance.memory || {}).usedJSHeapSize / 1048576)};
}"""


def text(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def inflated(task: dict, n: int, rng: random.Random, comments: int) -> dict:
    live = task.get("state") != "done"
    agents = [f"engineer@3231-{k}" for k in range(40)]
    now = int(time.time() * 1000)
    return {
        **task,
        "title": f"Task {n} {text(rng, 8)}",
        "description": text(rng, 380 if live else 30),
        "kind": "code",
        "rank": rng.choice(["urgent", "high", "normal", "normal", "low"]),
        "claimed_by": rng.choice(agents) if task.get("state") in {"pr", "claimed"} else "",
        "comments": [
            {"id": f"c{n}-{k}", "by": rng.choice(agents), "at": now - rng.randint(0, DAYS_MS), "text": text(rng, 45)}
            for k in range(comments if live else 2)
        ],
        "contract": {"done_when": text(rng, 30)},
    }


def seeded(comments: int) -> dict:
    """The corpus ledger with comments per live task, as the server sends it to the page."""
    corpus = json.load(gzip.open(FIXTURE))
    rng = random.Random(7)
    now = int(time.time() * 1000)
    agents = [f"engineer@3231-{k}" for k in range(40)]
    state = {
        "title": "rig-grade-swarm",
        "overview": text(rng, 220),
        "plans": [{**p, "title": f"Plan {p['id']}"} for p in corpus["plans"]],
        "phases": [
            {**p, "title": f"Phase {p['id']}", "description": text(rng, 120), "done": n % 3 == 0}
            for n, p in enumerate(corpus["phases"])
        ],
        "slices": [{**s, "anchor": f"s-{s['id'][:4]}"} for s in corpus["slices"]],
        "tasks": [inflated(task, n, rng, comments) for n, task in enumerate(corpus["tasks"])],
        "freezes": [{**f, "at": now} for f in corpus["freezes"]],
        "followups": [{"id": f"f{n}", "text": text(rng, 20), "done": n % 2 == 0} for n in range(300)],
        "questions": [],
        "notes": [],
        "chat": [{"id": f"m{n}", "by": "operator", "at": now, "text": text(rng, 20)} for n in range(60)],
        "_meta": {
            "rev": 1000,
            "events": [
                {"kind": "comment", "by": agents[k % 40], "at": now - k * 1000, "rev": 1000 - k, "target": "tasks/x"}
                for k in range(200)
            ],
            "crew": [{"name": a, "last_seen": now, "handled_rev": 990} for a in agents],
            "created_at": now - 86_400_000,
        },
    }
    view, closed = ledger_closed.trimmed(state)
    state.update(view)
    state["_meta"]["closed"] = closed
    return served_ledger(state)


def changed(task: dict, step: int, rng: random.Random) -> dict:
    kind = step % 4
    if kind < 2:
        note = {"id": f"live-{step}", "by": "engineer@3231-1", "at": int(time.time() * 1000), "text": f"live {step}"}
        return {**task, "comments": [*task["comments"], note]}
    if kind == 2:
        return {**task, "state": NEXT_STATE[task.get("state", "open")]}
    return {**task, "rank": rng.choice(["urgent", "high", "normal", "low"])}


def changes(state: dict, count: int, seed: int) -> list:
    """The server patches of count successive live changes, each to one live task."""
    rng = random.Random(seed)
    live = [t["id"] for t in state["tasks"] if t.get("state") != "done"]
    patches, current = [], state
    for step in range(count):
        target = rng.choice(live)
        tasks = [changed(t, step, rng) if t["id"] == target else t for t in current["tasks"]]
        meta = copy.copy(current["_meta"])
        meta["rev"] += 1
        event = {"kind": "comment", "by": "engineer@3231-1", "at": int(time.time() * 1000), "rev": meta["rev"]}
        meta["events"] = [*meta["events"][1:], {**event, "target": f"tasks/{target}"}]
        nxt = {**current, "tasks": tasks, "_meta": meta}
        patches.append(diff(current, nxt))
        current = nxt
    return patches


def replay(page: Any, state: dict, patches: list, gap: int) -> dict:
    """Apply each patch gap milliseconds apart on an open page and read what the main thread did."""
    args = {"initial": json.dumps(state), "patches": patches, "gap": gap, "open": list(OPEN)}
    result = page.evaluate(REPLAY, args)
    renders = sorted(c["render"] for c in result["perChange"])
    rows = sorted(c["rows"] for c in result["perChange"])
    return {
        "snapshot_mb": round(len(args["initial"]) / 1048576, 1),
        "changes": len(patches),
        "gap_ms": gap,
        "long_tasks": result["longTasks"],
        "longest_task_ms": result["longest"],
        "blocked_ms": result["blocked"],
        "render_ms_median": renders[len(renders) // 2],
        "render_ms_max": renders[-1],
        "rows_rebuilt_median": rows[len(rows) // 2],
        "rows_rebuilt_max": rows[-1],
        "open_section_ms": result["openMs"],
        "elements": result["elements"],
        "heap_mb": result["heapMB"],
    }


def opened(browser: Any, throttle: float) -> tuple[Any, Any]:
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger={"title": "seed", "_meta": {"rev": 1}})
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": throttle})
    return page, cdp


def hotspots(profile: dict) -> list:
    """Self time in milliseconds per function of a CPU profile, largest first."""
    nodes = {n["id"]: n for n in profile["nodes"]}
    totals: dict = {}
    deltas = [*profile["timeDeltas"][1:], 0]
    for sid, delta in zip(profile["samples"], deltas, strict=True):
        frame = nodes[sid]["callFrame"]
        key = f"{frame['functionName'] or '(anonymous)'} {frame['url'].rsplit('/', 1)[-1]}:{frame['lineNumber'] + 1}"
        totals[key] = totals.get(key, 0) + delta
    return [[key, round(us / 1000)] for key, us in sorted(totals.items(), key=lambda kv: -kv[1])[:40]]


def measure(args: argparse.Namespace) -> dict:
    from playwright.sync_api import sync_playwright

    state = seeded(args.comments)
    patches = changes(state, args.changes, args.seed)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, cdp = opened(browser, args.throttle)
        if args.profile:
            cdp.send("Profiler.enable")
            cdp.send("Profiler.start")
        reading = replay(page, state, patches, args.gap)
        if args.profile:
            Path(args.profile).write_text(json.dumps(hotspots(cdp.send("Profiler.stop")["profile"]), indent=1))
        if args.shot:
            page.screenshot(path=args.shot)
        browser.close()
    return {**reading, "cpu_throttle": args.throttle}


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a burst of live ledger changes and read the main thread.")
    parser.add_argument("--changes", type=int, default=30)
    parser.add_argument("--gap", type=int, default=1000, help="milliseconds between changes")
    parser.add_argument("--comments", type=int, default=40, help="comments on each live task")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--throttle", type=float, default=4, help="CPU slowdown factor")
    parser.add_argument("--shot", default="", help="screenshot path after the burst")
    parser.add_argument("--profile", default="", help="path for the CPU profile self time list")
    print(json.dumps(measure(parser.parse_args()), indent=1))


if __name__ == "__main__":
    main()
