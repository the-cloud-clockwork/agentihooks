"""agentihooks swarm: run a swarm of Claude and Codex agents over a swarm ledger in herdr.

agentihooks swarm list | tick | templates
agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N]
agentihooks swarm <id> start | pause | stop [--now] | status
agentihooks swarm <id> names [--json]                              the swarm code, its herdr space and every agent name it gave
agentihooks swarm <id> url                                        print the ledger page link (create and start print it last)
agentihooks swarm <id> close [--note TEXT] [--now]                 a live master writes the note first; then summary, snapshot, all retired
agentihooks swarm <id> reopen                                     keep the summary and settings, start a fresh master
agentihooks swarm <id> take-master [--replace]                    this session becomes the master and prints its priming
agentihooks swarm <id> remove                                     drop a swarm with no agents left, and its activity counts
agentihooks swarm <id> snapshot | restore [--from FILE]           save the swarm's state to its folder (stop does too); restore the newest, paused
agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N compact-limit=N   (or just: swarm <id> max-eng-agents=N)
agentihooks swarm <id> set snapshot-minutes=N                      automatic snapshot interval while running (default 30, 0 off)
agentihooks swarm <id> set codex-share=PCT codex-min-week-left=PCT   share of auto lane spawns sent to Codex (default 30, 5)
agentihooks swarm <id> set eng-agent=claude|codex|auto eng-model=M eng-effort=E eng-kind=K eng-role=TEXT   (ci- likewise)
agentihooks swarm <id> save-template NAME                         write this swarm's lanes, caps and compact limit as a template
agentihooks swarm <id> send-message TEXT                          operator message to the swarm chat
agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]     master or operator judges a health finding
agentihooks swarm <id> learned                                    list every seat's learned notes with seat and number
agentihooks swarm <id> promote SEAT NUMBER insight|canon --reason TEXT   raise a learned note; canon only by master or operator
agentihooks swarm <id> culture set FILE | show                    the swarm's shared culture, read by every new occupant
agent side (name from --as or AGENTIHOOKS_AGENT_NAME):
agentihooks swarm <id> issue URL | pr URL | done [--pr URL] | block NOTE | handoff DOC [--recap FILE] [--reason R] | say TEXT [--to NAME|eng|ci]
agentihooks swarm <id> learned TEXT [--maturity data|note|insight|canon]   (default note; canon only by the master)
agentihooks swarm <id> wait MINUTES [--reason TEXT]                 the tick counts no idle tick while it holds
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
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from scripts.doctor import priming
from scripts.gates import Who
from scripts.gates.identity import refusal
from scripts.handoff import check as handoff_check
from scripts.handoff import envelope as handoff_envelope
from scripts.handoff import transfers
from scripts.handoff.resolve import Resolver
from scripts.inbox import exits, wake
from scripts.inbox.seats import CANON, DEFAULT_MATURITY, MATURITIES, SeatError, is_seat, seat_address
from scripts.inbox.seats import PREFIX as SEAT_PREFIX
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import (
    control_notifications,
    delivery,
    idle,
    ledger_events,
    naming,
    phase_planning,
    phase_state,
    phases,
    plan_review,
    priority_sweep,
    prompt,
    snapshot,
    take_master,
    templates,
    timer,
)
from scripts.swarm.health import activity
from scripts.swarm.health import findings as health
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.runtime import HerdrRuntime, _bin
from scripts.swarm.status import auto_snapshot, findings, status_report, task_counts, verdict_store
from scripts.swarm.store import ASSIST, AUTONOMY, DELEGATE, MASTER, SwarmConfig, SwarmError, codex_split, connect
from scripts.swarm.tick import agent_status, primed, tick
from scripts.swarm_ledger import ledger_creator, ledger_kinds, ledger_link, plan_shape

SETTABLE = {
    "max-eng-agents": "max_eng",
    "max-ci-agents": "max_ci",
    "max-plan-agents": "max_plan",
    "compact-limit": "compact_limit",
    "codex-share": "codex_share",
    "codex-min-week-left": "codex_min_week_left",
    "snapshot-minutes": "snapshot_minutes",
}
LANE_KEYS = {f"{lane}-{key}": (lane, key) for lane in templates.LANES for key in templates.LANE_FIELDS}
TICK_LOCK_MS = 10 * 60 * 1000
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ONLY_MASTER_CANON = "only the master or the operator makes a learned note canon"
CLOSE_ASK = (
    "The operator pressed Close on the ledger page. Write one short paragraph in plain words on where the work "
    'stands, then run: agentihooks swarm {slug} close --note "<your paragraph>". Close retires you too.'
)


def now_ms():
    return int(time.time() * 1000)


def run_tick(store, slug, ledger=None, runtime=None, messenger=None):
    ledger = ledger or LedgerClient()
    if ledger.binned(slug):
        return ["the ledger is in the bin, skipped"]
    lock, token = store.key(slug, "tick-lock"), uuid.uuid4().hex
    if not store.redis.set(lock, token, nx=True, px=TICK_LOCK_MS):
        return ["another tick is running"]
    try:
        inbox = InboxStore(store.redis)
        doc = ledger.state(slug)
        actions = phase_planning.planning_pass(inbox, store, slug, doc, ledger, store.config(slug))
        if actions:
            doc = ledger.state(slug)
        ticked = phases.phase_pass(inbox, store, slug, doc, ledger)
        actions += ticked
        if ticked:
            actions += phase_planning.planning_pass(inbox, store, slug, ledger.state(slug), ledger, store.config(slug))
        actions += tick(slug, store, ledger, runtime or HerdrRuntime(), now_ms())
        if store.config(slug).template == "doctor":
            from scripts.doctor import cli as doctor

            actions += doctor.timer(store, slug, now_ms())
        herdr = messenger or delivery.HerdrMessenger()
        delivery.migrate_outbox(store, slug, inbox)
        agents = [a for a in store.agents(slug) if a.state != "finished"]
        delivery.relay_to_page(inbox, slug, agents, ledger)
        doc, config = ledger.state(slug), store.config(slug)
        actions += ledger_events.event_pass(inbox, store, slug, doc, ledger, now_ms())
        actions += priority_sweep.priority_pass(store, slug, doc, ledger)
        found = findings(store, slug, config, doc.get("tasks", []), doc.get("_meta", {}).get("events", []))
        actions += ledger_events.findings_pass(inbox, store, slug, found)
        window = wake.window_ms(os.environ)
        actions += wake.wake_pass(inbox, slug, agents, herdr, ledger, now_ms(), window, wake.quiet_ms(os.environ))
        taken = snapshot.auto(store, slug, now_ms(), os.environ)
        store.redis.set(store.key(slug, "last-tick"), now_ms())
        return actions + ([f"took automatic snapshot {taken.name}"] if taken else [])
    finally:
        if store.redis.get(lock) == token:
            store.redis.delete(lock)


def cmd_list(store, args):
    for slug in store.slugs():
        c = store.config(slug)
        print(
            f"{slug}\t{c.state}\teng {c.max_eng}\tci {c.max_ci}\tplan {c.max_plan}\tagents {len(store.agents(slug))}\t{c.repo}"
        )


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
    tasks = ledger_creator.swarm_tasks(ledger.state(args.slug))
    refused = ledger_creator.swarm_refusal(os.environ) or (
        args.template != priming.TEMPLATE and ledger_creator.floor_refusal(os.environ, tasks, args.operator_asked)
    )
    if refused:
        raise SwarmError(refused)
    ledger.mark_swarm(args.slug)
    caps = {key: value.cap for key, value in template.lanes.items()}
    config = SwarmConfig(
        args.slug,
        repo,
        caps["eng"] if args.max_eng_agents is None else args.max_eng_agents,
        caps["ci"] if args.max_ci_agents is None else args.max_ci_agents,
        max_plan=caps["plan"] if args.max_plan_agents is None else args.max_plan_agents,
        state="paused",
        compact_limit=template.compact_limit,
        template=args.template,
        lanes=templates.lane_map(template),
        links=template.links,
        autonomy=template.autonomy or DELEGATE,
    )
    store.create(config)
    print(json.dumps({"created": args.slug, "repo": repo, "state": "paused", "template": args.template}))
    print(ledger_link.page_line(args.slug))


def _state(store, args, state):
    if state == "running":
        store.redis.delete(store.key(args.slug, "master-retired-tasks"))
    store.update(args.slug, state=state)
    if state == "running" and not timer.ensure(_bin()):
        print("warning: the systemd timer could not be enabled; run agentihooks swarm tick yourself", file=sys.stderr)
    for action in run_tick(store, args.slug):
        print(action)
    print(json.dumps({"swarm": args.slug, "state": store.config(args.slug).state}))


def cmd_start(store, args):
    shape = plan_shape.report(LedgerClient().tasks(args.slug), store.config(args.slug).max_eng)
    print(shape["summary"], flush=True)
    if shape["warning"]:
        print(f"warning: {shape['warning']}", file=sys.stderr, flush=True)
    _state(store, args, "running")
    print(ledger_link.page_line(args.slug))


def cmd_url(store, args):
    print(ledger_link.page_line(args.slug))


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
    config = store.update(args.slug, state="stopping" if left else "stopped")
    if not left:
        runtime.close_space(config)
    print(json.dumps({"swarm": args.slug, "state": config.state, "still_running": left}))


def _live_master(store, slug, live):
    return next((a for a in store.agents(slug) if a.lane == MASTER and a.state != "finished" and a.name in live), None)


def _retire_each(store, slug, runtime, live, agents):
    left = []
    for agent in agents:
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)
        if not runtime.retire(agent, agent.name in live):
            store.put_agent(slug, agent)
            left.append(agent.name)
    return left


def cmd_close(store, args):
    store.config(args.slug)
    runtime = HerdrRuntime()
    live = runtime.live_names()
    by = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME") or "operator"
    master = None if args.now else _live_master(store, args.slug, live)
    if master is not None and master.name != by:
        InboxStore(store.redis).send("operator", master.name, CLOSE_ASK.format(slug=args.slug))
        print(json.dumps({"swarm": args.slug, "asked": master.name}))
        return
    ledger = LedgerClient()
    ledger.summarize(args.slug, args.note, by)
    path = snapshot.take(store, args.slug, now_ms())
    store.update(args.slug, state="stopping")
    agents = store.agents(args.slug)
    left = _retire_each(store, args.slug, runtime, live, [a for a in agents if a.lane != MASTER])
    for row in ledger.tasks(args.slug):
        if row.get("state") == "claimed":
            ledger.update_task(args.slug, row["id"], {"state": "open", "claimed_by": ""})
    ledger.mark_closed(args.slug, by)
    store.update(args.slug, state="stopping" if left else "stopped")
    print(json.dumps({"closed": args.slug, "snapshot": str(path), "still_running": left}), flush=True)
    if _retire_each(store, args.slug, runtime, live, [a for a in agents if a.lane == MASTER]):
        store.update(args.slug, state="stopping")


def cmd_reopen(store, args):
    runtime = HerdrRuntime()
    live = runtime.live_names()
    if args.slug not in store.slugs():
        snapshot.recreate(store, args.slug, live)
    if any(a.name in live for a in store.agents(args.slug)):
        raise SwarmError(f"swarm {args.slug} still has live agents; wait for close to finish")
    for agent in store.agents(args.slug):
        store.release(args.slug, agent.task, agent.name)
        store.drop_agent(args.slug, agent.name)
    by = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME") or "operator"
    LedgerClient().reopen(args.slug, by)
    _state(store, args, "running")


def cmd_take_master(store, args):
    runtime = HerdrRuntime()
    if args.slug not in store.slugs():
        snapshot.recreate(store, args.slug, runtime.live_names())
    name = os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    record = take_master.take(store, args.slug, name, runtime, now_ms(), args.replace)
    ledger = LedgerClient()
    if ledger.closed(args.slug):
        ledger.reopen(args.slug, record.name)
    if store.config(args.slug).state in ("stopped", "stopping"):
        store.update(args.slug, state="running")
        timer.ensure(_bin())
    config = store.config(args.slug)
    task = {"id": MASTER, "handoff": store.handoff(args.slug, MASTER), "peer": store.peer(args.slug)}
    print(
        prompt.build_master(
            args.slug, config.repo, record.name, primed(store, args.slug, record.seat, task), config.autonomy
        )
    )
    store.clear_handoff(args.slug, MASTER)


def cmd_set(store, args):
    changes, lanes = {}, {key: dict(value) for key, value in store.config(args.slug).lanes.items()}
    for pair in args.pairs:
        key, _, value = pair.partition("=")
        if key in LANE_KEYS:
            lane, field = LANE_KEYS[key]
            lanes.setdefault(lane, {})[field] = value
            changes["lanes"] = templates.lane_map(templates.parse({"name": "set", "lanes": lanes}))
            continue
        if key == "autonomy":
            changes["autonomy"] = value
            continue
        if key not in SETTABLE or not value.isdigit():
            raise SwarmError(
                f"set takes {', '.join(SETTABLE)}=<whole number>, autonomy={'|'.join(AUTONOMY)} "
                f"or {', '.join(LANE_KEYS)}=<value>"
            )
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
                "max_plan": config.max_plan,
                "compact_limit": config.compact_limit,
                "autonomy": config.autonomy,
                "codex_share": config.codex_share,
                "codex_min_week_left": config.codex_min_week_left,
                "snapshot_minutes": config.snapshot_minutes,
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
    path = templates.save(templates.from_config(args.template_name, store.config(args.slug)), os.environ)
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
    source = Path(args.source).expanduser() if args.source else snapshot.newest(args.slug)
    herdr = HerdrRuntime()
    outcomes = snapshot.restore(store, args.slug, herdr.live_names(), source, runtime=herdr)
    for action in run_tick(store, args.slug):
        print(action)
    state = store.config(args.slug).state
    restored = [asdict(o) for o in outcomes]
    print(json.dumps({"swarm": args.slug, "state": state, "snapshot": str(source), "restored": restored}))


def _share(store, config):
    spawns = store.spawns(config.slug)
    codex, total = spawns.get("codex", 0), sum(spawns.values())
    share, floor = codex_split(config, os.environ)
    return (
        f"codex {codex}/{total} spawns {codex * 100 // total if total else 0}%  target {share}%  min week left {floor}%"
    )


def _snapshot_line(auto):
    every = f"every {auto['every_minutes']} min" if auto["every_minutes"] > 0 else "automatic snapshots off"
    if auto["last"] is None:
        return f"snapshots  no automatic snapshot yet  {every}"
    taken = datetime.fromtimestamp(auto["last"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"snapshots  last automatic snapshot {taken}  {every}  kept {auto['kept']}"


def cmd_status(store, args):
    if args.json:
        store.config(args.slug)
        print(json.dumps(status_report(store, args.slug, LedgerClient().state(args.slug))))
        return
    config = store.config(args.slug)
    agents = store.agents(args.slug)
    ledger = LedgerClient()
    doc = ledger.state(args.slug)
    tasks = doc["tasks"]
    counts = task_counts(tasks)
    found = findings(store, args.slug, config, tasks, ledger.events(args.slug))
    print(
        f"{config.slug}  {config.state}  eng {config.max_eng}  ci {config.max_ci}  plan {config.max_plan}  repo {config.repo}  {_share(store, config)}"
    )
    print("tasks  " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    print(plan_shape.report(tasks, config.max_eng)["summary"])
    for phase_id, state, held in phase_state.report(doc):
        print(f"phase {phase_id}  {state}" + (f"  holds {', '.join(held)}" if held else ""))
    print(_snapshot_line(auto_snapshot(config)))
    print("Agent\tLane\tHarness\tProfile\tModel\tAccount\tPane\tTask\tState\tConversation\tModel source\tConfidence")
    for a in agents:
        model = " ".join(filter(None, (a.model, a.effort))) if a.model else "unknown"
        print(
            f"{a.name}\t{a.lane}\t{a.harness}\t{a.profile or 'unknown'}\t{model}\t{a.account or '-'}\t{a.pane_id}\t{a.task}\t{agent_status(a)}\t{a.conversation_id or '-'}\t{a.model_source or '-'}\t{a.model_confidence if a.model_confidence is not None else '-'}"
        )
    for r in store.restored(args.slug):
        print(f"restored  {r['name']}  {r['outcome']}  {r['reason']}")
    for f in found:
        print(f"finding  {f['kind']}  {f['subject']}: {f['summary']}")
        for entry in f["evidence"]:
            print(f"  - {entry}")
        print(f"  threshold {f['threshold']}")
        print(f"  id {f['id']}" + (f"  earlier verdict {f['verdict']['value']}" if f["verdict"] else ""))


def cmd_names(store, args):
    config = store.ensure_code(args.slug)
    rows = store.names.names(args.slug)
    if args.json:
        print(
            json.dumps(
                {"code": config.code, "space": naming.space(config.repo, config.code, config.slug), "names": rows}
            )
        )
        return
    print(f"code {config.code}\tspace {naming.space(config.repo, config.code, config.slug)}")
    for row in rows:
        retired = row["retired_at"] or "-"
        print(
            f"{row['name']}\t{row['type']}\t{row['number']}\t{row['session_id'] or '-'}\t{row['spawned_at']}\t{retired}"
        )


def cmd_rename(store, args):
    from scripts.herdr_host import HerdrError
    from scripts.swarm.rename import rename_swarm

    slugs = [args.slug] if getattr(args, "slug", "") else store.slugs()
    ledger, runtime, failed = LedgerClient(), HerdrRuntime(), []
    for slug in slugs:
        if not store.agents(slug):
            continue
        try:
            for action in rename_swarm(store, slug, ledger, runtime, now_ms()):
                print(f"{slug}: {action}")
        except (SwarmError, HerdrError) as exc:
            failed.append(f"{slug}: {exc}")
    if failed:
        raise SwarmError("; ".join(failed))


def cmd_verdict(store, args):
    store.config(args.slug)
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent is not None and agent.lane != MASTER:
        raise SwarmError("only the master or the operator gives a finding a verdict")
    verdict = verdict_store(store, args.slug).judge(args.finding, args.verdict, args.note, name or "operator", now_ms())
    minutes = health.limits().cooldown_minutes
    print(json.dumps({"finding": args.finding, "verdict": verdict["value"], "hidden_minutes": minutes}))


def cmd_send_message(store, args):
    store.config(args.slug)
    LedgerClient().say(args.slug, args.text)
    print(json.dumps({"posted": True}))


def _me(store, args):
    name = store.names.resolve(args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", ""))
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
    awaiting = "approval" if store.config(args.slug).autonomy == ASSIST else ""
    fields = {"pr_url": args.url, "state": "pr", "awaiting": awaiting}
    LedgerClient().update_task(args.slug, agent.task, fields, by=agent.name)
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
    _retire(store, args.slug, agent, "finished its task and exited")
    print(json.dumps({"task": agent.task, "state": "done", "next": "stop now; the swarm closes this session"}))


def cmd_block(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    ledger.comment(args.slug, agent.task, args.note, by=agent.name)
    ledger.update_task(args.slug, agent.task, {"state": "blocked"}, by=agent.name)
    _retire(store, args.slug, agent, "blocked its task and exited")
    print(json.dumps({"task": agent.task, "state": "blocked", "next": "stop now; the swarm closes this session"}))


def cmd_wait(store, args):
    agent = _me(store, args)
    if args.minutes <= 0:
        raise SwarmError("a wait lasts a whole number of minutes above zero")
    at = now_ms()
    until = at + args.minutes * 60_000
    idle.declare_wait(store.redis, args.slug, agent.name, until, args.reason, at)
    print(json.dumps({"agent": agent.name, "until": datetime.fromtimestamp(until / 1000, timezone.utc).isoformat()}))


def cmd_plan(store, args):
    agent, autonomy = _me(store, args), store.config(args.slug).autonomy
    result = plan_review.decide(LedgerClient(), args.slug, agent, autonomy, args.phase, args.action, args.note)
    print(json.dumps(result))


def cmd_handoff(store, args):
    agent = _me(store, args)
    text = _read(args.doc, "handoff document")
    recap = "\n\n".join(
        f"## {heading}\n{handoff_check.section(text, heading)}" for heading in ("Done", "Stopped at", "Next")
    )
    ledger = LedgerClient()
    found = handoff_check.problems(text, Resolver(args.slug, store.redis, ledger.state))
    if found:
        raise SwarmError(handoff_check.refusal(found))
    envelope = handoff_envelope.build(store, args.slug, agent, args.reason, _ledger_rows(ledger, args.slug), now_ms())
    store.memory.add_recap(_seat(agent), agent.name, agent.task, recap, now_ms())
    store.put_handoff(args.slug, agent.task, text, seat=agent.seat, envelope=envelope)
    transfer = transfers.record(store, args.slug, agent, args.reason, text, now_ms())
    store.put_agent(args.slug, replace(agent, state="finished"))
    exits.settle(InboxStore(store.redis), agent.name, agent.seat, "handed off its seat")
    print(
        json.dumps(
            {
                "task": agent.task,
                "state": "handoff",
                "envelope": envelope,
                "transfer": transfer,
                "next": "stop now; a successor continues from your document",
            }
        )
    )


def cmd_confirm_handoff(store, args):
    agent = _me(store, args)
    if agent.name not in HerdrRuntime().live_names():
        raise SwarmError("Only a live successor can confirm its handoff")
    result = transfers.confirm(store, args.slug, args.transfer, agent, args.next, now_ms())
    print(json.dumps(result))


def cmd_restore_decision(store, args):
    from scripts.swarm import resume

    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    if name not in {"", "operator"} and _me(store, args).lane != MASTER:
        raise SwarmError("Only the master or operator can choose resume or fresh")
    outcome = resume.decide(store, args.slug, args.agent, args.choice, HerdrRuntime(), now_ms(), LedgerClient())
    print(json.dumps(asdict(outcome)))


def _ledger_rows(ledger, slug):
    try:
        return ledger.tasks(slug)
    except SwarmError:
        return None


def cmd_learned(store, args):
    if not args.text:
        _list_learned(store, args.slug)
        return
    agent = _me(store, args)
    if args.maturity == CANON and agent.lane != MASTER:
        raise SwarmError(ONLY_MASTER_CANON)
    if not re.fullmatch(r".*\w.*\bbecause\b.*\w.*", args.text, re.IGNORECASE | re.DOTALL):
        raise SwarmError("a learned note needs a because clause with a reason")
    store.memory.learn(_seat(agent), agent.name, args.text, now_ms(), args.maturity)
    print(json.dumps({"seat": agent.seat, "learned": args.text, "maturity": args.maturity}))


def _list_learned(store, slug):
    store.config(slug)
    for key in store.seats.swarm_keys(slug):
        seat, _, kind = key.removeprefix(f"{SEAT_PREFIX}:").partition(":")
        if kind != "learned":
            continue
        for number, note in enumerate(store.memory.learned(seat), 1):
            print(f"{seat}\t{number}\t{note['maturity']}\t{note['text']}")


def cmd_promote(store, args):
    store.config(args.slug)
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if args.maturity == CANON and agent is not None and agent.lane != MASTER:
        raise SwarmError(ONLY_MASTER_CANON)
    seat = args.seat if is_seat(args.seat) else seat_address(args.slug, args.seat)
    if not seat.endswith(f"@{args.slug}"):
        raise SwarmError(f"{args.seat} is not a seat of swarm {args.slug}")
    try:
        entry = store.memory.promote(seat, args.number, args.maturity, name or "operator", args.reason, now_ms())
    except SeatError as exc:
        raise SwarmError(str(exc)) from exc
    print(json.dumps({"seat": seat, "number": args.number, "maturity": entry["maturity"]}))


def cmd_culture(store, args):
    store.config(args.slug)
    if args.action == "set":
        store.culture.set(args.slug, _read(args.file, "culture file"))
        print(json.dumps({"swarm": args.slug, "culture": "set"}))
        return
    text = store.culture.get(args.slug)
    if not text:
        print(f"swarm {args.slug} has no culture yet; write one with culture set FILE", file=sys.stderr)
    print(text, end="")


def _read(path, what):
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the {what} {path}: {exc.strerror}") from exc


def _seat(agent):
    if not agent.seat:
        raise SwarmError(f"{agent.name} holds no seat, so there is nowhere to keep this")
    return agent.seat


def _retire(store, slug, agent, exit_text):
    store.release(slug, agent.task, agent.name)
    store.put_agent(slug, replace(agent, state="finished"))
    exits.settle(InboxStore(store.redis), agent.name, "", exit_text)


def cmd_say(store, args):
    agent = _me(store, args)
    text = f"@{args.to} {args.text}" if args.to in ("eng", "ci") else args.text
    if args.to:
        delivery.send(store, args.slug, args.text, sender=agent.name, to=args.to, fyi=args.fyi)
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
    create.add_argument("--max-plan-agents", type=int, default=None)
    create.add_argument("--operator-asked")
    for plain in ("start", "pause", "remove", "snapshot", "url", "reopen", "rename"):
        sub.add_parser(plain)
    sub.add_parser("restore").add_argument("--from", dest="source", default="")
    sub.add_parser("stop").add_argument("--now", action="store_true")
    close = sub.add_parser("close")
    close.add_argument("--note", default="")
    close.add_argument("--now", action="store_true")
    sub.add_parser("take-master").add_argument("--replace", action="store_true")
    sub.add_parser("set").add_argument("pairs", nargs="+")
    sub.add_parser("save-template").add_argument("template_name", metavar="name")
    sub.add_parser("status").add_argument("--json", action="store_true")
    sub.add_parser("names").add_argument("--json", action="store_true")
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
    plan = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    for action in plan_review.DECISIONS:
        decision = plan.add_parser(action)
        decision.add_argument("phase")
        decision.add_argument("--note", required=action == "send-back")
    wait = sub.add_parser("wait")
    wait.add_argument("minutes", type=int)
    wait.add_argument("--reason", default="")
    handoff = sub.add_parser("handoff")
    handoff.add_argument("doc")
    handoff.add_argument("--recap", default="")
    handoff.add_argument("--reason", choices=handoff_envelope.REASONS, default="recycle")
    confirm = sub.add_parser("confirm-handoff")
    confirm.add_argument("transfer")
    confirm.add_argument("--next", required=True)
    decision = sub.add_parser("restore-decision")
    decision.add_argument("agent")
    decision.add_argument("choice", choices=("resume", "fresh"))
    learned = sub.add_parser("learned")
    learned.add_argument("text", nargs="?", default="")
    learned.add_argument("--maturity", choices=MATURITIES, default=DEFAULT_MATURITY)
    promote = sub.add_parser("promote")
    promote.add_argument("seat")
    promote.add_argument("number", type=int)
    promote.add_argument("maturity", choices=MATURITIES)
    promote.add_argument("--reason", required=True)
    culture = sub.add_parser("culture").add_subparsers(dest="action", required=True)
    culture.add_parser("set").add_argument("file")
    culture.add_parser("show")
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--to", default="")
    say.add_argument("--fyi", action="store_true")
    return parser


def main(argv):
    if argv and argv[0] in ("list", "tick", "templates", "rename"):
        handler, args = globals()[f"cmd_{argv[0]}"], argparse.Namespace()
    else:
        if len(argv) > 1 and argv[1].partition("=")[0] in (*SETTABLE, *LANE_KEYS, "autonomy"):
            argv = [argv[0], "set", *argv[1:]]
        args = build_parser().parse_args(argv)
        handler = globals()[f"cmd_{args.command.replace('-', '_')}"]
    if text := refusal(vars(args).get("name"), Who.from_env()):
        print(f"swarm: {text}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        store = connect()
        action = getattr(args, "command", "")
        before = control_notifications.master(store, args.slug) if action in control_notifications.CONTROLS else None
        handler(store, args)
        if action in control_notifications.CONTROLS:
            control_notifications.notify(store, args, LedgerClient(), before, action)
    except (SwarmError, InboxError) as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return 1
    return 0
