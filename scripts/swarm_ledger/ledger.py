#!/usr/bin/env python3
"""Agent CLI for a ledger: join the crew, talk in chat, record progress, acknowledge operator events.

Usage: ledger.py [--slug SLUG] [--as NAME] <command> [args]      (env fallbacks PLAN_LEDGER, PLAN_LEDGER_AS)

  join [--role orchestrator|member]   enter the crew (the gate binds this session)
  leave                               leave the crew
  status                              crew, my unhandled operator events
  events                              my unhandled operator events, one line each
  ack [--rev N]                       mark operator events up to N (default: latest) as handled
  say TEXT [--long]                   chat message (TEXT "-" reads stdin); --long only when the operator asked to expand
  comment ITEM TEXT                   your status on phases/<id>, questions/<id> or followups/<id>; amends your last one
  phase ID done|open [--status T]     set a phase state, T becomes your status comment
  followup add TEXT | done|open ID    add a follow-up, close one, or reopen one
  scope ITEM in|out [--status T]      mark an item out of scope (or back in); T says why
  retext ITEM TEXT                    rewrite the text of a follow-up or question
  edit chat|ITEM ENTRY TEXT           rewrite an entry (yours; the orchestrator: any agent's)
  delete chat|ITEM ENTRY...           delete entries (yours; the orchestrator: any agent's)
  audit                               list every agent text the filter refuses, the cleanup worklist
  priority add ITEM TEXT              ask the operator: only what blocks on his answer, at most 20 words
  priority clear ID... | --all        clear priorities once answered
  time-left DURATION                 record remaining time, e.g. "3h 20m"
  claim ITEM                          take ownership of an item's operator events
  task add ID TITLE --lane eng|ci [--phase P] [--description D]   add a swarm task
  task set ID FIELD=VALUE...          set state, claimed_by, issue_url or pr_url of a swarm task
  prompt                              print the join paragraph for a launch prompt

Agent text is for the operator: plain words, what was done or why it was skipped. The server refuses
clock times, dates, hashes, run ids, file names, code identifiers, capital labels, dashes, arrows,
AI phrasing, more than one parenthesis or semicolon, and comments over 50 words (chat 100, items 40).

Env: LEDGER_DIR, LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765).
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_comments  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import watch_ledger  # noqa: E402

BASE = f"http://{os.environ.get('LEDGER_HOST', '127.0.0.1')}:{os.environ.get('LEDGER_PORT', '8765')}"


def request(slug, ops=None):
    token = core.read_token(core.paths(slug)[0].read_text(encoding="utf-8")) or ""
    headers = {"Content-Type": "application/json", "X-Ledger-Token": token}
    body = None if ops is None else json.dumps({"ops": ops}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/{slug}", data=body, headers=headers, method="GET" if ops is None else "PUT"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def call(slug, ops=None):
    try:
        return request(slug, ops)
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {exc.read().decode(errors='replace')}")
    except OSError:
        subprocess.run([sys.executable, str(HERE / "ledger_server.py"), "--ensure"], check=False, capture_output=True)
    try:
        return request(slug, ops)
    except (OSError, urllib.error.HTTPError) as exc:
        sys.exit(f"ledger server not answering on {BASE}: {exc}")


def op(kind, args, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": args.name, **fields}


def send(args, kind, **fields):
    state = call(args.slug, [op(kind, args, **fields)])
    if state.get("rejected"):
        sys.exit(f"rejected: {state['rejected']}")
    return state


def mine(state, name):
    return gate.unhandled_for(state["_meta"], name)


def cmd_join(args):
    send(args, "join", role=args.role)
    print(json.dumps({"joined": args.name, "slug": args.slug, "role": args.role}))


def cmd_leave(args):
    send(args, "leave")
    print(json.dumps({"left": args.name}))


def cmd_events(args):
    for event in mine(call(args.slug), args.name):
        print(watch_ledger.line(event))


def cmd_status(args):
    state = call(args.slug)
    print(
        json.dumps(
            {
                "crew": state["_meta"].get("crew", []),
                "unhandled": len(mine(state, args.name)),
                "rev": state["_meta"]["rev"],
                "time_left_minutes": state.get("time_left_minutes"),
            },
            indent=2,
        )
    )


def cmd_ack(args):
    rev = args.rev if args.rev is not None else call(args.slug)["_meta"]["rev"]
    send(args, "ack", rev=rev)
    print(json.dumps({"acked": rev}))


def cmd_say(args):
    text = (sys.stdin.read() if args.text == "-" else args.text).strip()
    op = {"op": "add", "thread": "chat", "id": f"m-{uuid.uuid4().hex[:10]}", "text": text, "by": args.name}
    if args.long:
        op["long"] = True
    state = call(args.slug, [op])
    print(json.dumps({"posted": not state.get("rejected")}))


def cmd_comment(args):
    thread = f"{args.item}/comments"
    state = call(
        args.slug,
        [{"op": "add", "thread": thread, "id": f"c-{uuid.uuid4().hex[:10]}", "text": args.text, "by": args.name}],
    )
    print(json.dumps({"posted": not state.get("rejected")}))


def cmd_phase(args):
    send(args, "set", **with_status(args, path=f"phases/{args.id}/done", value=args.state == "done"))
    print(json.dumps({"phase": args.id, "state": args.state}))


def with_status(args, **fields):
    return {**fields, "status": args.status} if args.status else fields


def cmd_followup(args):
    if args.action == "add":
        send(args, "add_item", list="followups", text=args.value)
    else:
        send(args, "set", **with_status(args, path=f"followups/{args.value}/done", value=args.action == "done"))
    print(json.dumps({"followup": args.action}))


def cmd_priority(args):
    if args.action == "add":
        if len(args.values) != 2:
            sys.exit('priority add needs ITEM and "TEXT"')
        send(args, "priority", item=args.values[0], text=args.values[1])
        print(json.dumps({"priority": args.values[0]}))
        return
    targets = ["all"] if args.all else args.values
    state = call(args.slug, [op("priority_clear", args, target=t) for t in targets])
    print(json.dumps({"cleared": targets, "rejected": state.get("rejected", [])}))


def cmd_scope(args):
    send(args, "set", **with_status(args, path=f"{args.item}/out_of_scope", value=args.state == "out"))
    print(json.dumps({"item": args.item, "scope": args.state}))


def cmd_retext(args):
    send(args, "retext", item=args.item, text=args.text)
    print(json.dumps({"retext": args.item}))


def thread_of(target):
    return "chat" if target == "chat" else f"{target}/comments"


def cmd_edit(args):
    state = call(
        args.slug,
        [{"op": "edit", "thread": thread_of(args.target), "id": args.entry, "text": args.text, "by": args.name}],
    )
    if state.get("rejected"):
        sys.exit(f"rejected: {state['rejected']} (not yours, the operator's, or missing)")
    print(json.dumps({"edited": args.entry}))


def cmd_delete(args):
    ops = [{"op": "delete", "thread": thread_of(args.target), "id": entry, "by": args.name} for entry in args.entries]
    state = call(args.slug, ops)
    print(
        json.dumps(
            {
                "deleted": [e for e in args.entries if e not in state.get("rejected", [])],
                "rejected": state.get("rejected", []),
            }
        )
    )


def cmd_audit(args):
    rows = ledger_comments.audit(call(args.slug))
    for where, entry, by, reasons in rows:
        print(f"{where} {entry} {by or '-'}: {'; '.join(reasons)}")
    print(f"{len(rows)} to clean")


def _duration(value):
    minutes = core.time_left_value(value)
    if minutes is None:
        raise argparse.ArgumentTypeError("time left must be a duration such as 3h 20m")
    return minutes


def cmd_time_left(args):
    send(args, "set", path="time_left_minutes", value=args.minutes)
    print(json.dumps({"time_left_minutes": args.minutes}))


def cmd_claim(args):
    send(args, "claim", item=args.item)
    print(json.dumps({"claimed": args.item}))


def cmd_task(args):
    if args.action == "add":
        title = " ".join(args.values)
        send(
            args, "task_add", task=args.id, title=title, lane=args.lane, phase=args.phase, description=args.description
        )
        print(json.dumps({"task": args.id, "added": title}))
        return
    fields = dict(value.split("=", 1) for value in args.values if "=" in value)
    if len(fields) != len(args.values):
        sys.exit("task set takes FIELD=VALUE pairs")
    send(args, "task_update", item=f"tasks/{args.id}", fields=fields)
    print(json.dumps({"task": args.id, **fields}))


def cmd_prompt(args):
    me = "agentihooks ledger"
    print(
        f"You are a member of crew ledger `{args.slug}` as `{args.name}`. Run once: {me} --slug {args.slug} "
        f"--as {args.name} join. Then keep a Monitor on: {me} watch {args.slug} --as {args.name}. Act on every OPERATOR "
        f"line, then run `{me} --slug {args.slug} --as {args.name} ack`. Record progress with the commands in "
        f"`{me} --help` (phase, followup, comment, scope, say, time-left). The orchestrator maintains Time Left as one remaining duration "
        'with `time-left "3h 20m"` on joining and whenever progress or blockers change it. Write for the operator in plain words: '
        "one status comment per item saying what was done or why it was skipped, amended instead of repeated; evidence stays "
        "in PRs and notes. A hook blocks you from stopping while operator events are unhandled."
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", default=os.environ.get("PLAN_LEDGER"))
    parser.add_argument("--as", dest="name", default=os.environ.get("PLAN_LEDGER_AS"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("join").add_argument("--role", choices=["orchestrator", "member"], default="member")
    for plain in ("leave", "status", "events", "prompt"):
        sub.add_parser(plain)
    sub.add_parser("ack").add_argument("--rev", type=int)
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--long", action="store_true")
    for name, first, second in (("comment", "item", "text"), ("retext", "item", "text")):
        parser_ = sub.add_parser(name)
        parser_.add_argument(first)
        parser_.add_argument(second)
    phase = sub.add_parser("phase")
    phase.add_argument("id")
    phase.add_argument("state", choices=["done", "open"])
    phase.add_argument("--status")
    followup = sub.add_parser("followup")
    followup.add_argument("action", choices=["add", "done", "open"])
    followup.add_argument("value")
    followup.add_argument("--status")
    scope = sub.add_parser("scope")
    scope.add_argument("item")
    scope.add_argument("state", choices=["in", "out"])
    scope.add_argument("--status")
    edit = sub.add_parser("edit")
    for arg in ("target", "entry", "text"):
        edit.add_argument(arg)
    delete = sub.add_parser("delete")
    delete.add_argument("target")
    delete.add_argument("entries", nargs="+")
    sub.add_parser("audit")
    priority = sub.add_parser("priority")
    priority.add_argument("action", choices=["add", "clear"])
    priority.add_argument("values", nargs="*")
    priority.add_argument("--all", action="store_true")
    sub.add_parser("time-left").add_argument("minutes", type=_duration)
    sub.add_parser("claim").add_argument("item")
    task = sub.add_parser("task")
    task.add_argument("action", choices=["add", "set"])
    task.add_argument("id")
    task.add_argument("values", nargs="+")
    task.add_argument("--lane", choices=["eng", "ci"], default="eng")
    task.add_argument("--phase", default="")
    task.add_argument("--description", default="")
    return parser


def main():
    args = build_parser().parse_args()
    if not args.slug or not args.name:
        sys.exit("--slug and --as are required (or env PLAN_LEDGER and PLAN_LEDGER_AS)")
    globals()[f"cmd_{args.command.replace('-', '_')}"](args)


if __name__ == "__main__":
    main()
