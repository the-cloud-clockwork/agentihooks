"""agentihooks swarm: run a swarm of Claude and Codex agents over a swarm ledger in herdr.

agentihooks swarm list | tick
agentihooks swarm <id> create --repo DIR [--max-eng-agents N] [--max-ci-agents N]
agentihooks swarm <id> start | pause | stop [--now] | status
agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N compact-limit=N   (or just: swarm <id> max-eng-agents=N)
agentihooks swarm <id> send-message TEXT                          operator message to the swarm chat
agent side (name from --as or AGENTIHOOKS_AGENT_NAME):
agentihooks swarm <id> issue URL | pr URL | done [--pr URL] | block NOTE | handoff DOC | say TEXT [--to NAME|eng|ci]
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import delivery, timer
from scripts.swarm.health import activity
from scripts.swarm.health import findings as health
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.runtime import HerdrRuntime, _bin
from scripts.swarm.store import MASTER, SwarmConfig, SwarmError, connect
from scripts.swarm.tick import agent_status, tick

SETTABLE = {"max-eng-agents": "max_eng", "max-ci-agents": "max_ci", "compact-limit": "compact_limit"}
TICK_LOCK_MS = 10 * 60 * 1000
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


def now_ms():
    return int(time.time() * 1000)


def run_tick(store, slug, ledger=None, runtime=None, messenger=None):
    lock, token = store.key(slug, "tick-lock"), uuid.uuid4().hex
    if not store.redis.set(lock, token, nx=True, px=TICK_LOCK_MS):
        return ["another tick is running"]
    ledger = ledger or LedgerClient()
    try:
        actions = tick(slug, store, ledger, runtime or HerdrRuntime(), now_ms())
        herdr = messenger or delivery.HerdrMessenger()
        delivery.relay_operator_chat(store, slug, ledger.chat(slug), herdr)
        actions += [f"delivered to {name}" for name in delivery.flush(store, slug, herdr)]
        agents = [a for a in store.agents(slug) if a.state != "finished"]
        window = wake.window_ms(os.environ)
        return actions + wake.wake_pass(InboxStore(store.redis), slug, agents, herdr, ledger, now_ms(), window)
    finally:
        if store.redis.get(lock) == token:
            store.redis.delete(lock)


def cmd_list(store, args):
    for slug in store.slugs():
        c = store.config(slug)
        print(f"{slug}\t{c.state}\teng {c.max_eng}\tci {c.max_ci}\tagents {len(store.agents(slug))}\t{c.repo}")


def cmd_tick(store, args):
    for slug in store.slugs():
        try:
            for action in run_tick(store, slug):
                print(f"{slug}: {action}")
        except Exception as exc:
            print(f"{slug}: {type(exc).__name__}: {exc}", file=sys.stderr)


def cmd_create(store, args):
    if not SLUG_RE.match(args.slug):
        raise SwarmError("a swarm id is lowercase letters, digits and dashes, starting with a letter, at most 48 long")
    repo = os.path.abspath(os.path.expanduser(args.repo))
    ledger = LedgerClient()
    ledger.tasks(args.slug)
    store.create(SwarmConfig(args.slug, repo, args.max_eng_agents, args.max_ci_agents, state="paused"))
    delivery.start_cursor(store, args.slug, ledger.chat(args.slug))
    print(json.dumps({"created": args.slug, "repo": repo, "state": "paused"}))


def _state(store, args, state):
    store.update(args.slug, state=state)
    if state == "running" and not timer.ensure(_bin()):
        print("warning: the systemd timer could not be enabled; run agentihooks swarm tick yourself", file=sys.stderr)
    for action in run_tick(store, args.slug):
        print(action)
    print(json.dumps({"swarm": args.slug, "state": store.config(args.slug).state}))


def cmd_start(store, args):
    _state(store, args, "running")


def cmd_pause(store, args):
    _state(store, args, "paused")


def cmd_stop(store, args):
    if not args.now:
        _state(store, args, "stopping")
        return
    store.update(args.slug, state="stopping")
    runtime, ledger = HerdrRuntime(), LedgerClient()
    rows = {t["id"]: t for t in ledger.tasks(args.slug)}
    live, left = runtime.live_names(), []
    for agent in store.agents(args.slug):
        if not runtime.retire(agent, agent.name in live):
            left.append(agent.name)
            continue
        store.release(args.slug, agent.task, agent.name)
        store.drop_agent(args.slug, agent.name)
        row = rows.get(agent.task, {})
        if agent.state != "finished" and row.get("state") in ("claimed", "pr") and row.get("claimed_by") == agent.name:
            ledger.update_task(args.slug, agent.task, {"state": "open", "claimed_by": ""})
    store.update(args.slug, state="stopping" if left else "stopped")
    print(json.dumps({"swarm": args.slug, "state": store.config(args.slug).state, "still_running": left}))


def cmd_set(store, args):
    changes = {}
    for pair in args.pairs:
        key, _, value = pair.partition("=")
        if key not in SETTABLE or not value.isdigit():
            raise SwarmError(f"set takes {', '.join(SETTABLE)}=<whole number>")
        changes[SETTABLE[key]] = int(value)
    config = store.update(args.slug, **changes)
    if config.state == "running":
        for action in run_tick(store, args.slug):
            print(action)
    print(
        json.dumps(
            {
                "swarm": args.slug,
                "max_eng": config.max_eng,
                "max_ci": config.max_ci,
                "compact_limit": config.compact_limit,
            }
        )
    )


def cmd_status(store, args):
    config = store.config(args.slug)
    agents = store.agents(args.slug)
    ledger = LedgerClient()
    tasks = ledger.tasks(args.slug)
    counts = {s: sum(1 for t in tasks if t.get("state") == s) for s in ("open", "claimed", "blocked", "pr", "done")}
    found = health.findings(
        {"tasks": tasks, "_meta": {"events": ledger.events(args.slug)}},
        [a.__dict__ for a in agents],
        activity.counts(args.slug),
        now_ms(),
        health.limits(),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "config": config.__dict__,
                    "agents": [{**a.__dict__, "status": agent_status(a)} for a in agents],
                    "tasks": counts,
                    "findings": [f.as_dict() for f in found],
                }
            )
        )
        return
    print(f"{config.slug}  {config.state}  eng {config.max_eng}  ci {config.max_ci}  repo {config.repo}")
    print("tasks  " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    for a in agents:
        model = " ".join(filter(None, (a.model, a.effort))) if a.model else "unknown"
        print(f"{a.name}\t{a.lane}\t{a.harness}\t{model}\t{a.account or '-'}\t{a.pane_id}\t{a.task}\t{a.state}")
    for f in found:
        print(f"finding  {f.kind}  {f.subject}: {f.evidence}; threshold {f.threshold}")


def cmd_send_message(store, args):
    store.config(args.slug)
    ledger = LedgerClient()
    ledger.say(args.slug, args.text)
    delivery.relay_operator_chat(store, args.slug, ledger.chat(args.slug), delivery.HerdrMessenger())
    print(json.dumps({"posted": True}))


def _me(store, args):
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent is None:
        raise SwarmError(f"{name or 'this session'} is not an agent of swarm {args.slug}")
    return agent


def _worker(store, args):
    agent = _me(store, args)
    if agent.lane == MASTER:
        raise SwarmError(f"{agent.name} is the swarm master; the master works no task")
    return agent


def cmd_issue(store, args):
    agent = _worker(store, args)
    LedgerClient().update_task(args.slug, agent.task, {"issue_url": args.url}, by=agent.name)
    print(json.dumps({"task": agent.task, "issue_url": args.url}))


def cmd_pr(store, args):
    agent = _worker(store, args)
    LedgerClient().update_task(args.slug, agent.task, {"pr_url": args.url, "state": "pr"}, by=agent.name)
    print(json.dumps({"task": agent.task, "pr_url": args.url}))


def cmd_done(store, args):
    agent = _worker(store, args)
    fields = {"state": "done", **({"pr_url": args.pr} if args.pr else {})}
    LedgerClient().update_task(args.slug, agent.task, fields, by=agent.name)
    _retire(store, args.slug, agent)
    print(json.dumps({"task": agent.task, "state": "done", "next": "stop now; the swarm closes this session"}))


def cmd_block(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    ledger.comment(args.slug, agent.task, args.note, by=agent.name)
    ledger.update_task(args.slug, agent.task, {"state": "blocked"}, by=agent.name)
    _retire(store, args.slug, agent)
    print(json.dumps({"task": agent.task, "state": "blocked", "next": "stop now; the swarm closes this session"}))


def cmd_handoff(store, args):
    agent = _me(store, args)
    try:
        text = Path(args.doc).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the handoff document {args.doc}: {exc.strerror}") from exc
    store.put_handoff(args.slug, agent.task, text)
    store.put_agent(args.slug, replace(agent, state="finished"))
    print(
        json.dumps(
            {"task": agent.task, "state": "handoff", "next": "stop now; a successor continues from your document"}
        )
    )


def _retire(store, slug, agent):
    store.release(slug, agent.task, agent.name)
    store.put_agent(slug, replace(agent, state="finished"))


def cmd_say(store, args):
    agent = _me(store, args)
    text = f"@{args.to} {args.text}" if args.to in ("eng", "ci") else args.text
    LedgerClient().say(args.slug, text, by=agent.name)
    delivery.send(store, args.slug, args.text, sender=agent.name, to=args.to, herdr=delivery.HerdrMessenger())
    print(json.dumps({"posted": True}))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentihooks swarm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("slug")
    parser.add_argument("--as", dest="name", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--repo", required=True)
    create.add_argument("--max-eng-agents", type=int, default=2)
    create.add_argument("--max-ci-agents", type=int, default=1)
    for plain in ("start", "pause"):
        sub.add_parser(plain)
    sub.add_parser("stop").add_argument("--now", action="store_true")
    sub.add_parser("set").add_argument("pairs", nargs="+")
    sub.add_parser("status").add_argument("--json", action="store_true")
    sub.add_parser("send-message").add_argument("text")
    for name in ("issue", "pr"):
        sub.add_parser(name).add_argument("url")
    sub.add_parser("done").add_argument("--pr", default="")
    sub.add_parser("block").add_argument("note")
    sub.add_parser("handoff").add_argument("doc")
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--to", default="")
    return parser


def main(argv):
    if argv and argv[0] in ("list", "tick"):
        handler, args = globals()[f"cmd_{argv[0]}"], argparse.Namespace()
    else:
        if len(argv) > 1 and argv[1].partition("=")[0] in SETTABLE:
            argv = [argv[0], "set", *argv[1:]]
        args = build_parser().parse_args(argv)
        handler = globals()[f"cmd_{args.command.replace('-', '_')}"]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        handler(connect(), args)
    except SwarmError as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return 1
    return 0
