"""agentihooks msg: durable messages between any two sessions.

agentihooks msg send ADDRESS TEXT...        everything after the address is the text
agentihooks msg inbox                       this session's items with their state
agentihooks msg read ID                     show an item with its history, mark it read
agentihooks msg close ID done|handoff ADDRESS|blocked WHAT|cancel [WHY]

The sender is this session: AGENTIHOOKS_AGENT_NAME, else CLAUDE_CODE_SESSION_ID.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict

from scripts.inbox.store import InboxError, connect


def identity(environ=None):
    env = os.environ if environ is None else environ
    me = env.get("AGENTIHOOKS_AGENT_NAME", "") or env.get("CLAUDE_CODE_SESSION_ID", "")
    if not me:
        raise InboxError("this session has no identity: set AGENTIHOOKS_AGENT_NAME or run inside a Claude Code session")
    return me


def cmd_send(store, me, args):
    item = store.send(me, args.address, " ".join(args.text))
    print(json.dumps({"id": item.id, "from": item.sender, "to": item.address, "state": item.state}))


def cmd_inbox(store, me, args):
    for item in store.inbox(me):
        print(f"{item.id}\t{item.state}\t{item.sender}\t{item.text.splitlines()[0]}")


def cmd_read(store, me, args):
    item = store.read(args.id, me)
    print(json.dumps({**asdict(item), "history": store.history(item.id)}))


def cmd_close(store, me, args):
    item = store.close(args.id, me, args.kind, " ".join(args.detail))
    print(json.dumps({"id": item.id, "state": item.state, "reason": item.reason}))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentihooks msg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    send = sub.add_parser("send")
    send.add_argument("address")
    send.add_argument("text", nargs=argparse.REMAINDER)
    send.set_defaults(func=cmd_send)
    sub.add_parser("inbox").set_defaults(func=cmd_inbox)
    read = sub.add_parser("read")
    read.add_argument("id")
    read.set_defaults(func=cmd_read)
    close = sub.add_parser("close")
    close.add_argument("id")
    close.add_argument("kind", nargs="?", default="")
    close.add_argument("detail", nargs=argparse.REMAINDER)
    close.set_defaults(func=cmd_close)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        me = identity()
        args.func(connect(), me, args)
    except InboxError as exc:
        print(f"agentihooks msg: {exc}", file=sys.stderr)
        return 1
    return 0
