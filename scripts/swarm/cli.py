"""agentihooks swarm: run a swarm of Claude and Codex agents over a swarm ledger in herdr.

agentihooks swarm list | tick | templates
agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N]
agentihooks swarm <id> start | pause | stop [--now] | status
agentihooks swarm <id> remove                                     drop a swarm with no agents left, and its activity counts
agentihooks swarm <id> snapshot | restore                         save the swarm's state to its folder (stop does too); restore it paused
agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N compact-limit=N   (or just: swarm <id> max-eng-agents=N)
agentihooks swarm <id> set eng-agent=claude|codex|auto eng-model=M eng-effort=E eng-kind=K eng-role=TEXT   (ci- likewise)
agentihooks swarm <id> save-template NAME                         write this swarm's lanes, caps and compact limit as a template
agentihooks swarm <id> send-message TEXT                          operator message to the swarm chat
agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]     master or operator judges a health finding
agent side (name from --as or AGENTIHOOKS_AGENT_NAME):
agentihooks swarm <id> issue URL | pr URL | done [--pr URL] | block NOTE | handoff DOC [--recap FILE] | learned TEXT | say TEXT [--to NAME|eng|ci]
done carries the proof its task's kind needs: ops and tune --command C --output O; troubleshoot --root-cause R
--evidence E with --fix URL or --filed TASK; research --finding URL
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
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import delivery, snapshot, templates, timer
from scripts.swarm.health import activity, checks, verdicts
from scripts.swarm.health import findings as health
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.runtime import HerdrRuntime, _bin
from scripts.swarm.store import MASTER, SwarmConfig, SwarmError, connect
from scripts.swarm.tick import agent_status, tick
from scripts.swarm_ledger import ledger_kinds

SETTABLE = {"max-eng-agents": "max_eng", "max-ci-agents": "max_ci", "compact-limit": "compact_limit"}
LANE_KEYS = {f"{lane}-{key}": (lane, key) for lane in templates.LANES for key in templates.LANE_FIELDS}
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
        inbox = InboxStore(store.redis)
        delivery.migrate_outbox(store, slug, inbox)
        agents = [a for a in store.agents(slug) if a.state != "finished"]
        delivery.relay_to_page(inbox, slug, agents, ledger)
        window = wake.window_ms(os.environ)
        return actions + wake.wake_pass(inbox, slug, agents, herdr, ledger, now_ms(), window)
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
    template = templates.load(args.template, os.environ) if args.template else templates.parse({"name": "none"})
    ledger = LedgerClient()
    ledger.tasks(args.slug)
    caps = {key: value.cap for key, value in template.lanes.items()}
    config = SwarmConfig(
        args.slug,
        repo,
        caps["eng"] if args.max_eng_agents is None else args.max_eng_agents,
        caps["ci"] if args.max_ci_agents is None else args.max_ci_agents,
        state="paused",
        compact_limit=template.compact_limit,
        template=args.template,
        lanes=templates.lane_map(template),
        links=template.links,
    )
    store.create(config)
    print(json.dumps({"created": args.slug, "repo": repo, "state": "paused", "template": args.template}))


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
    _snapshot(store, args.slug)
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
    changes, lanes = {}, {key: dict(value) for key, value in store.config(args.slug).lanes.items()}
    for pair in args.pairs:
        key, _, value = pair.partition("=")
        if key in LANE_KEYS:
            lane, field = LANE_KEYS[key]
            lanes.setdefault(lane, {})[field] = value
            changes["lanes"] = templates.lane_map(templates.parse({"name": "set", "lanes": lanes}))
            continue
        if key not in SETTABLE or not value.isdigit():
            raise SwarmError(f"set takes {', '.join(SETTABLE)}=<whole number> or {', '.join(LANE_KEYS)}=<value>")
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
                "lanes": config.lanes,
            }
        )
    )


def cmd_templates(store, args):
    for template, source in templates.available(os.environ):
        lanes = "\t".join(
            f"{key} {lane.cap} {lane.agent} {lane.model} {lane.effort} {lane.kind}"
            for key, lane in template.lanes.items()
        )
        print(f"{template.name}\t{source}\t{lanes}\tcompact {template.compact_limit}")


def cmd_save_template(store, args):
    config = store.config(args.slug)
    try:
        source = templates.load(config.template, os.environ) if config.template else None
    except SwarmError:
        source = None
    path = templates.save(templates.from_config(args.template_name, config, source), os.environ)
    print(json.dumps({"swarm": args.slug, "template": args.template_name, "path": str(path)}))


def cmd_remove(store, args):
    store.remove(args.slug)
    activity.clear(args.slug)
    print(json.dumps({"removed": args.slug}))


def _snapshot(store, slug):
    names = [a.name for a in store.agents(slug)]
    path = snapshot.take(store, slug, now_ms())
    return {"swarm": slug, "snapshot": str(path), "agents": names}


def cmd_snapshot(store, args):
    print(json.dumps(_snapshot(store, args.slug)))


def cmd_restore(store, args):
    finished = snapshot.restore(store, args.slug, HerdrRuntime().live_names())
    for action in run_tick(store, args.slug):
        print(action)
    print(json.dumps({"swarm": args.slug, "state": store.config(args.slug).state, "finished": finished}))


def cmd_status(store, args):
    config = store.config(args.slug)
    agents = store.agents(args.slug)
    ledger = LedgerClient()
    tasks = ledger.tasks(args.slug)
    counts = {s: sum(1 for t in tasks if t.get("state") == s) for s in ("open", "claimed", "blocked", "pr", "done")}
    rows, limits = [a.__dict__ for a in agents], health.limits()
    found = _verdicts(store, args.slug).visible(
        health.findings(
            {"tasks": tasks, "_meta": {"events": ledger.events(args.slug)}},
            rows,
            activity.counts(args.slug),
            now_ms(),
            limits,
            checks.waiting(rows, tasks, limits, checks.cached(store.redis, store.key(args.slug, "checks"))),
        ),
        now_ms(),
        limits.cooldown_minutes * 60_000,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "config": config.__dict__,
                    "agents": [{**a.__dict__, "status": agent_status(a)} for a in agents],
                    "tasks": counts,
                    "findings": found,
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
        print(f"finding  {f['kind']}  {f['subject']}: {f['summary']}")
        for entry in f["evidence"]:
            print(f"  - {entry}")
        print(f"  threshold {f['threshold']}")
        print(f"  id {f['id']}" + (f"  earlier verdict {f['verdict']['value']}" if f["verdict"] else ""))


def _verdicts(store, slug):
    return verdicts.VerdictStore(store.redis, store.key(slug, "findings"))


def cmd_verdict(store, args):
    store.config(args.slug)
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent is not None and agent.lane != MASTER:
        raise SwarmError("only the master or the operator gives a finding a verdict")
    verdict = _verdicts(store, args.slug).judge(args.finding, args.verdict, args.note, name or "operator", now_ms())
    minutes = health.limits().cooldown_minutes
    print(json.dumps({"finding": args.finding, "verdict": verdict["value"], "hidden_minutes": minutes}))


def cmd_send_message(store, args):
    store.config(args.slug)
    LedgerClient().say(args.slug, args.text)
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
    ledger = LedgerClient()
    row = next((t for t in ledger.tasks(args.slug) if t.get("id") == agent.task), {})
    proof = {key: getattr(args, f"proof_{key}") for key in ledger_kinds.PROOF_KEYS if getattr(args, f"proof_{key}")}
    missing = ledger_kinds.unmet({**row, "proof": {**(row.get("proof") or {}), **proof}})
    if missing:
        flags = ", ".join("--" + key.replace("_", "-").replace(" or ", " or --") for key in missing)
        raise SwarmError(f"a {ledger_kinds.kind(row)} task is done only with its proof: give {flags}")
    fields = {"state": "done", **({"pr_url": args.pr} if args.pr else {}), **({"proof": proof} if proof else {})}
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
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
    text = _read(args.doc, "handoff document")
    recap = _read(args.recap, "recap") if args.recap else ""
    if recap:
        store.memory.add_recap(_seat(agent), agent.name, agent.task, recap, now_ms())
    store.put_handoff(args.slug, agent.task, text, seat=agent.seat)
    store.put_agent(args.slug, replace(agent, state="finished"))
    print(
        json.dumps(
            {"task": agent.task, "state": "handoff", "next": "stop now; a successor continues from your document"}
        )
    )


def cmd_learned(store, args):
    agent = _me(store, args)
    store.memory.learn(_seat(agent), agent.name, args.text, now_ms())
    print(json.dumps({"seat": agent.seat, "learned": args.text}))


def _read(path, what):
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the {what} {path}: {exc.strerror}") from exc


def _seat(agent):
    if not agent.seat:
        raise SwarmError(f"{agent.name} holds no seat, so there is nowhere to keep this")
    return agent.seat


def _retire(store, slug, agent):
    store.release(slug, agent.task, agent.name)
    store.put_agent(slug, replace(agent, state="finished"))


def cmd_say(store, args):
    agent = _me(store, args)
    text = f"@{args.to} {args.text}" if args.to in ("eng", "ci") else args.text
    if args.to:
        delivery.send(store, args.slug, args.text, sender=agent.name, to=args.to)
    LedgerClient().say(args.slug, text, by=agent.name)
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
    create.add_argument("--template", default="")
    create.add_argument("--max-eng-agents", type=int, default=None)
    create.add_argument("--max-ci-agents", type=int, default=None)
    for plain in ("start", "pause", "remove", "snapshot", "restore"):
        sub.add_parser(plain)
    sub.add_parser("stop").add_argument("--now", action="store_true")
    sub.add_parser("set").add_argument("pairs", nargs="+")
    sub.add_parser("save-template").add_argument("template_name", metavar="name")
    sub.add_parser("status").add_argument("--json", action="store_true")
    verdict = sub.add_parser("verdict")
    verdict.add_argument("finding")
    verdict.add_argument("verdict")
    verdict.add_argument("--note", default="")
    sub.add_parser("send-message").add_argument("text")
    for name in ("issue", "pr"):
        sub.add_parser(name).add_argument("url")
    done = sub.add_parser("done")
    done.add_argument("--pr", default="")
    for key in ledger_kinds.PROOF_KEYS:
        done.add_argument("--" + key.replace("_", "-"), dest=f"proof_{key}", default="")
    sub.add_parser("block").add_argument("note")
    handoff = sub.add_parser("handoff")
    handoff.add_argument("doc")
    handoff.add_argument("--recap", default="")
    sub.add_parser("learned").add_argument("text")
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--to", default="")
    return parser


def main(argv):
    if argv and argv[0] in ("list", "tick", "templates"):
        handler, args = globals()[f"cmd_{argv[0]}"], argparse.Namespace()
    else:
        if len(argv) > 1 and (argv[1].partition("=")[0] in SETTABLE or argv[1].partition("=")[0] in LANE_KEYS):
            argv = [argv[0], "set", *argv[1:]]
        args = build_parser().parse_args(argv)
        handler = globals()[f"cmd_{args.command.replace('-', '_')}"]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        handler(connect(), args)
    except (SwarmError, InboxError) as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return 1
    return 0
