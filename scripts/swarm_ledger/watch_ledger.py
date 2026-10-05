#!/usr/bin/env python3
"""Print one line per operator event on a ledger; the command an agent runs under Monitor.

Usage: watch_ledger.py <slug> [--as NAME] [--since-rev N] [--interval 3] [--all]

Reads <LEDGER_DIR>/<slug>.json every interval and prints each event logged after rev N
(default: the rev at start; --as NAME: only those the owner rule gives NAME; --all: every event, agents' too):

  OPERATOR rev=12 comment added on phases/p1 [c-1a2b]: "text"
  OPERATOR rev=13 comment edited on phases/p1 [c-1a2b] diff: "-old line\\n+new line"
  OPERATOR rev=14 comment deleted on phases/p1 [c-1a2b] was: "text"
  OPERATOR rev=15 answer added on questions/q3 [a-9f0e]: "text"
  OPERATOR rev=16 note added [n-77aa]: "text"
  OPERATOR rev=17 checked phases/p2
  OPERATOR rev=18 message added on chat [m-3c4d]: "text" | REPLY RULES: <chat_instructions>

plus `SEED_ERROR <message>` when the HTML seed stops parsing and `WARNING <message>`
when a word limit is broken. Restart with --since-rev <last rev printed> to replay.
"""

import argparse
import atexit
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402

from scripts.inbox import seen  # noqa: E402


def read(json_path):
    try:
        state = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and isinstance(state.get("_meta"), dict) else None


def line(event, rules=""):
    where = f" on {event['target']}" if event.get("target") and " " in event["kind"] else f" {event.get('target', '')}"
    head = f"{event['by'].upper() if event['by'] == 'operator' else event['by']} rev={event['rev']} {event['kind']}{where.rstrip()}"
    if "id" in event:
        head += f" [{event['id']}]"
    images = ""
    if event.get("image_paths"):
        images = (
            f" | Screenshots: {json.dumps(event['image_paths'], ensure_ascii=False)}."
            " Open each image: Claude use Read; Codex use view_image with the absolute path."
        )
    if "diff" in event:
        return f"{head} diff: {json.dumps(event['diff'], ensure_ascii=False)}{images}"
    if "text" in event:
        sep = " was:" if event["kind"].endswith("deleted") else ":"
        wrapped = (
            f" | REPLY RULES: {rules}"
            if rules and event.get("target") == "chat" and event["kind"].endswith("added")
            else ""
        )
        return f"{head}{sep} {json.dumps(event['text'], ensure_ascii=False)}{images}{wrapped}"
    return head + images


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--as", dest="name")
    parser.add_argument("--since-rev", type=int)
    args = parser.parse_args()
    json_path = core.paths(args.slug)[1]
    state = read(json_path)
    if state is None:
        sys.exit(f"no ledger JSON at {json_path}")
    since = state["_meta"]["rev"] if args.since_rev is None else args.since_rev
    print(f"WATCHING {json_path} rev {state['_meta']['rev']}", flush=True)
    seed_error, warned = None, []
    beat = core.watch_path(args.slug, args.name) if args.name else None
    marks = seen.marks_for(args.slug) if args.name else None
    if beat:
        atexit.register(beat.unlink, missing_ok=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        if beat:
            beat.parent.mkdir(parents=True, exist_ok=True)
            beat.touch()
        meta = state["_meta"]
        members, tasks = meta.get("members", {}), state.get("tasks", [])
        fresh = [
            e
            for e in meta.get("events", [])
            if e.get("rev", 0) > since
            and (args.all or (e.get("by") == "operator" and (not args.name or gate.owes(e, members, args.name, tasks))))
        ]
        for event in seen.first_showing(marks, args.name, args.slug, fresh):
            print(line(event, state.get("chat_instructions") or core.DEFAULT_CHAT_INSTRUCTIONS), flush=True)
        since = max(since, meta["rev"])
        if meta.get("seed_error") != seed_error:
            seed_error = meta.get("seed_error")
            print(f"SEED_ERROR {seed_error}" if seed_error else "SEED_OK", flush=True)
        for message in set(meta.get("warnings") or []) - set(warned):
            print(f"WARNING {message}", flush=True)
        warned = meta.get("warnings") or []
        time.sleep(args.interval)
        state = read(json_path) or state


if __name__ == "__main__":
    main()
