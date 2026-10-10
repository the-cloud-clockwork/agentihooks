"""agentihooks msg: durable messages between any two sessions.

agentihooks msg send ADDRESS [--fyi] TEXT...  everything after the address is the text
agentihooks msg inbox [--of ADDRESS]          this session's items with their state, or another seat's
agentihooks msg read ID                       show an item with its history, mark it read
agentihooks msg reply ID [--fyi] TEXT...      answer the item's sender and close the item done
agentihooks msg close ID done|handoff ADDRESS|blocked WHAT|cancel [WHY]

--fyi marks an item that needs no work (a thanks, a confirmation): its receiver closes it done with nothing to name.

The sender is this session: AGENTIHOOKS_AGENT_NAME, else CLAUDE_CODE_SESSION_ID.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict

from hooks.context.conditions import INBOX_SEND
from hooks.filters import check as filters
from scripts.inbox import links
from scripts.inbox.store import InboxError, InboxStore, connect
from scripts.swarm_ledger import ledger_comments

OPERATOR = "operator"

SWARM = "swarm"


FYI = "--fyi"


def identity(environ=None):
    env = os.environ if environ is None else environ
    me = env.get("AGENTIHOOKS_AGENT_NAME", "") or registered_name() or env.get("CLAUDE_CODE_SESSION_ID", "")
    if not me:
        raise InboxError("this session has no identity: set AGENTIHOOKS_AGENT_NAME or run inside a Claude Code session")
    return me


def registered_name():
    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import session_name

    return session_name(agent_pid())


def check_for_operator(address, text):
    """A message to the operator is shown in the ledger page chat, so it must pass the chat word rules now."""
    if address != OPERATOR:
        return
    try:
        ledger_comments.check(text, "chat")
    except ValueError as exc:
        raise InboxError(str(exc)) from exc


def informational(words: list[str]) -> tuple[bool, list[str]]:
    text = list(words)
    fyi = False
    if text[:1] == [FYI]:
        fyi, text = True, text[1:]
    if text[-1:] == [FYI]:
        fyi, text = True, text[:-1]
    return fyi, text


def cmd_send(store, me, args):
    from scripts.inbox.addresses import check_address

    check_address(store, me, args.address)
    links.check_send(store, me, args.address)
    fyi, words = informational(args.text)
    check_for_operator(args.address, " ".join(words))
    try:
        text = filters.screen(INBOX_SEND, " ".join(words))
    except ValueError as exc:
        raise InboxError(str(exc)) from exc
    item = store.send(me, args.address, text, fyi=fyi, task=store.receiver_task(args.address))
    print(json.dumps({"id": item.id, "from": item.sender, "to": item.address, "state": item.state}))


def cmd_inbox(store, me, args):
    if args.of:
        links.check_observe(store, me, args.of)
    for item in store.mailbox(args.of or me):
        print(f"{item.id}\t{item.state}\t{item.sender}\t{item.text.splitlines()[0]}")


def cmd_read(store, me, args):
    item = store.read(args.id, me)
    print(json.dumps({**asdict(item), "history": store.history(item.id)}))


def cmd_reply(store: InboxStore, me: str, args: argparse.Namespace) -> None:
    from scripts.inbox.addresses import check_address

    fyi, words = informational(args.text)
    if store.get(args.id).sender == SWARM:
        cmd_close(store, me, argparse.Namespace(id=args.id, kind="done", detail=words))
        return
    check_address(store, me, store.get(args.id).sender)
    check_for_operator(store.get(args.id).sender, " ".join(words))
    answer = store.reply(args.id, me, " ".join(words), fyi=fyi)
    print(json.dumps({"id": answer.id, "to": answer.address, "closed": args.id}))


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
    inbox = sub.add_parser("inbox")
    inbox.add_argument("--of", default="", metavar="ADDRESS")
    inbox.set_defaults(func=cmd_inbox)
    read = sub.add_parser("read")
    read.add_argument("id")
    read.set_defaults(func=cmd_read)
    reply = sub.add_parser("reply")
    reply.add_argument("id")
    reply.add_argument("text", nargs=argparse.REMAINDER)
    reply.set_defaults(func=cmd_reply)
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
