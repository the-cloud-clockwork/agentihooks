#!/usr/bin/env python3
"""Print one line per operator event on a ledger; the command an agent runs under Monitor.

Usage: watch_ledger.py <slug> [--as NAME] [--since-rev N] [--interval 3] [--all]

Follows the ledger server's event stream, one snapshot then only changes, and prints each event logged after
rev N (default: the rev at start; --as NAME: only those the owner rule gives NAME; --all: every event, agents'
too). A dropped stream reconnects after --interval seconds and replays from its cursor:

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
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import ledger_link  # noqa: E402

from scripts.inbox import seen  # noqa: E402
from scripts.swarm_ledger.events import Expired, patch  # noqa: E402
from scripts.swarm_ledger.events import stream as events_stream  # noqa: E402

STREAM_TIMEOUT_S = 3 * events_stream.HEARTBEAT_S


def credentials(slug):
    import ledger

    return ledger.credentials(slug)


def stream(slug, cursor=None, headers=None):
    """Yield (event, data, cursor) from the ledger server's event stream; Expired when the cursor is gone."""
    url, headers = request(slug, cursor, credentials(slug) if headers is None else headers)
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=STREAM_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            raise Expired from None
        raise
    with response:
        yield from events_stream.parse(line.decode() for line in response)


def request(slug, cursor, headers):
    """The events URL and headers: the credential headers, the stream Accept, and the cursor to resume after."""
    headers = {**headers, "Accept": "text/event-stream"}
    if cursor:
        headers["Last-Event-ID"] = cursor
    return f"{ledger_link.base()}/api/v1/ledgers/{urllib.parse.quote(slug, safe='')}/events", headers


def say(text):
    print(text, flush=True)


class Watch:
    """What one watcher has printed so far, so a reconnect or reset never prints an event twice."""

    def __init__(self, args, source, marks):
        self.args, self.source, self.marks = args, source, marks
        self.state, self.since = None, args.since_rev
        self.seed_error, self.warned = None, []

    def show(self, state):
        if self.state is None:
            say(f"WATCHING {self.source} rev {state['_meta']['rev']}")
            if self.since is None:
                self.since = state["_meta"]["rev"]
        self.state = state
        meta, args = state["_meta"], self.args
        members, tasks = meta.get("members", {}), state.get("tasks", [])
        fresh = [
            e
            for e in meta.get("events", [])
            if e.get("rev", 0) > self.since
            and (args.all or (e.get("by") == "operator" and (not args.name or gate.owes(e, members, args.name, tasks))))
        ]
        for event in seen.first_showing(self.marks, args.name, args.slug, fresh):
            say(line(event, state.get("chat_instructions") or core.DEFAULT_CHAT_INSTRUCTIONS))
        self.since = max(self.since, meta["rev"])
        if meta.get("seed_error") != self.seed_error:
            self.seed_error = meta.get("seed_error")
            say(f"SEED_ERROR {self.seed_error}" if self.seed_error else "SEED_OK")
        for message in set(meta.get("warnings") or []) - set(self.warned):
            say(f"WARNING {message}")
        self.warned = meta.get("warnings") or []

    def take(self, name, data):
        if name == "snapshot":
            self.show(data["ledger"])
        elif name == "ledger":
            self.show(patch.apply(self.state, data["patch"]))


def line(event, rules=""):
    where = f" on {event['target']}" if event.get("target") and " " in event["kind"] else f" {event.get('target', '')}"
    head = f"{event['by'].upper() if event['by'] == 'operator' else event['by']} rev={event['rev']} {event['kind']}{where.rstrip()}"
    if "id" in event:
        head += f" [{event['id']}]"
    if "note_text" in event:
        head += f" | Note: {json.dumps(event['note_text'], ensure_ascii=False)}"
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


def alive(beat):
    if beat:
        beat.parent.mkdir(parents=True, exist_ok=True)
        beat.touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--as", dest="name")
    parser.add_argument("--since-rev", type=int)
    args = parser.parse_args()
    from scripts.swarm_ledger.repository import repository

    if not repository.exists(args.slug):
        sys.exit(f"no ledger {args.slug} in {core.LEDGER_DIR}")
    beat = core.watch_path(args.slug, args.name) if args.name else None
    watch = Watch(args, f"ledger {args.slug}", seen.marks_for(args.slug) if args.name else None)
    if beat:
        atexit.register(beat.unlink, missing_ok=True)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    headers, cursor, failure = None, None, None
    while True:
        alive(beat)
        try:
            headers = headers or credentials(args.slug)
            for name, data, event_id in stream(args.slug, cursor, headers):
                alive(beat)
                watch.take(name, data)
                cursor, failure = event_id or cursor, None
        except Expired:
            cursor = None
            continue
        except (KeyError, TypeError):
            cursor = None
        except (OSError, ValueError) as exc:
            headers = None
            if str(exc) != failure:
                failure = str(exc)
                say(f"WARNING ledger stream: {failure}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
