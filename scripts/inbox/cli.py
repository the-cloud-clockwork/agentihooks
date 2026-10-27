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
from scripts.inbox import links, outbox
from scripts.inbox.store import DuplicateItem, InboxError, InboxStore, Unreachable, close_reason, connect
from scripts.swarm_ledger import ledger_comments
from scripts.swarm_v2 import master_fence

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


def check_for_operator(address, text, kind="chat"):
    """A message to the operator is shown on the ledger page, so it must pass its chat or comment word rules now."""
    if address != OPERATOR:
        return
    try:
        ledger_comments.check(text, kind)
    except ValueError as exc:
        raise InboxError(str(exc)) from exc


def check_answer(store, item_id, sender, text):
    """An answer to an operator item on a ledger entry lands as a comment there, under the comment word rules."""
    if sender == OPERATOR:
        check_for_operator(sender, text, "comment" if store.entry_of(item_id) else "chat")


def informational(words: list[str]) -> tuple[bool, list[str]]:
    text = list(words)
    fyi = False
    if text[:1] == [FYI]:
        fyi, text = True, text[1:]
    if text[-1:] == [FYI]:
        fyi, text = True, text[:-1]
    return fyi, text


def cmd_send(store, me, args):
    fyi, words = informational(args.text)
    check_for_operator(args.address, " ".join(words))
    try:
        text = filters.screen(INBOX_SEND, " ".join(words))
    except ValueError as exc:
        raise InboxError(str(exc)) from exc
    submit(store, outbox.record("send", me, {"address": args.address, "text": text, "fyi": fyi}))


def cmd_inbox(store, me, args):
    if args.of:
        links.check_observe(store, me, args.of)
    for item in store.mailbox(args.of or me):
        print(f"{item.id}\t{item.state}\t{item.sender}\t{item.text.splitlines()[0]}")


def cmd_read(store, me, args):
    item = store.read(args.id, me)
    print(json.dumps({**asdict(item), "history": store.history(item.id)}))


def cmd_reply(store: InboxStore, me: str, args: argparse.Namespace) -> None:
    fyi, words = informational(args.text)
    submit(store, outbox.record("reply", me, {"id": args.id, "text": " ".join(words), "fyi": fyi}))


def cmd_close(store, me, args):
    detail = " ".join(args.detail)
    close_reason(args.kind, detail)
    submit(store, outbox.record("close", me, {"kind": args.kind, "detail": detail}, item=args.id))


def submit(store, record):
    print(json.dumps(outbox.submit(store, record, apply)))


def apply(store: InboxStore, record: dict) -> dict:
    """Run one outbox record against Redis: the command itself, live or replayed."""
    return APPLY[record["op"]](store, record["by"], record)


def _apply_send(store, me, record):
    from scripts.inbox.addresses import check_address, dispatcher_status
    from scripts.inbox.recipient import empty_seat

    args = record["args"]
    address = _operator_door(store, me) if store.names.resolve(args["address"]) == OPERATOR else args["address"]
    check_address(store, me, address)
    links.check_send(store, me, address)
    status = dispatcher_status(store, address) if not args["fyi"] else {}
    address = status.get("dispatch", {}).get("owner", address)
    master_fence.check_send(store, me, address)
    hint = empty_seat(store, me, address)
    task = store.receiver_task(address)
    item = store.send(me, address, args["text"], fyi=args["fyi"], task=task, item_id=record["item"])
    result = {"id": item.id, "from": item.sender, "to": item.address, "state": item.state}
    return result | status | hint


def _operator_door(store, me):
    """Only the front door master writes to the operator, and never first: any other agent's message goes to it."""
    door, holds = store.front_door_of(me)
    if holds:
        raise InboxError(f"no agent writes to the operator first, so {me} cannot send to him; answer his item instead")
    return door or OPERATOR


def _apply_reply(store, me, record):
    from scripts.inbox.addresses import check_address

    args = record["args"]
    sender = store.get(args["id"]).sender
    if sender == SWARM:
        close = {"kind": "done", "detail": args["text"]}
        return _apply_close(store, me, {**record, "item": args["id"], "args": close})
    check_address(store, me, sender)
    master_fence.check_send(store, me, sender)
    check_answer(store, args["id"], sender, args["text"])
    answer = store.reply(args["id"], me, args["text"], fyi=args["fyi"], answer_id=record["item"])
    return {"id": answer.id, "to": answer.address, "closed": args["id"]}


def _apply_close(store, me, record):
    args = record["args"]
    item = store.get(record["item"])
    if (item.state, item.reason) == close_reason(args["kind"], args["detail"]) and _closed_by(store, item, me):
        raise DuplicateItem(f"message {item.id} is already closed: {item.reason}")
    item = store.close(record["item"], me, args["kind"], args["detail"])
    return {"id": item.id, "state": item.state, "reason": item.reason}


def _closed_by(store: InboxStore, item, me: str) -> bool:
    closing = [entry for entry in store.history(item.id) if "state" in entry][-1]
    return closing["state"] == item.state and store.names.resolve(closing["by"]) == store.names.resolve(me)


def _apply_say(store, me, record):
    from scripts.swarm import delivery

    return delivery.apply_say(store, me, record)


APPLY = {
    "send": _apply_send,
    "reply": _apply_reply,
    "close": _apply_close,
    "post": outbox.apply_post,
    "say": _apply_say,
    "ledger": outbox.apply_ledger,
}


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


def connected(cmd: str) -> InboxStore | None:
    try:
        return connect()
    except Unreachable:
        if cmd in APPLY:
            return None
        raise


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        me = identity()
        args.func(connected(args.cmd), me, args)
    except InboxError as exc:
        print(f"agentihooks msg: {exc}", file=sys.stderr)
        return 1
    return 0
