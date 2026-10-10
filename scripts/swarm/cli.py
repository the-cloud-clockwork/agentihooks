"""agentihooks swarm: run a swarm of Claude and Codex agents over a swarm ledger in herdr.

agentihooks swarm list | tick | templates
agentihooks swarm <id> create --repo DIR [--template NAME] [--max-eng-agents N] [--max-ci-agents N]
agentihooks swarm <id> start | pause | stop [--now] | status
agentihooks swarm <id> names [--json]                              the swarm code, its herdr space and every agent name it gave
agentihooks swarm <id> url                                        print the ledger page link (create and start print it last)
agentihooks swarm <id> close [--note TEXT] [--now]                 a live master writes the note first; then summary, snapshot, all retired
agentihooks swarm <id> reopen                                     keep the summary and settings, start a fresh master
agentihooks swarm <id> take-master [--replace]                    this session becomes the master and prints its priming
agentihooks swarm <id> master up [--last | --new]                 from a terminal: bring back the last master's conversation or start a new one
agentihooks swarm <id> <profile> up                               from a terminal: any role or overlay profile (planner, engineer, cicd, qa, frontend) in a pane for you, no task
agentihooks swarm <id> agent-up <profile>                         the same launch in its parser form
agentihooks swarm <id> remove                                     drop a swarm with no agents left, and its activity counts
agentihooks swarm <id> snapshot | restore [--from FILE]           save the swarm's state to its folder (stop does too); restore the newest, paused
agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N compact-limit=N   (or just: swarm <id> max-eng-agents=N)
agentihooks swarm <id> set snapshot-minutes=N                      automatic snapshot interval while running (default 30, 0 off)
agentihooks swarm <id> set eng-agent=claude|codex|auto eng-model=M eng-effort=E eng-kind=K eng-role=TEXT   (ci- likewise)
agentihooks swarm <id> set effort-min=E effort-max=E               every lane launch effort stays in this range (default medium, high)
agentihooks swarm <id> set master-agent=claude|codex              master affinity; a change orders the live master to hand off to that harness
agentihooks swarm <id> save-template NAME                         write this swarm's lanes, caps and compact limit as a template
agentihooks swarm <id> send-message TEXT                          message to every live agent's inbox
agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]     master or operator judges a health finding
agentihooks swarm <id> classify EXECUTION lost|working --reason TEXT  master or operator classifies a suspect execution attempt
agentihooks swarm <id> lift AGENT GATE                            operator or master lets one agent past a gate for one hour
agentihooks swarm <id> freeze | focus | unfreeze TARGET [--reason TEXT] [--quote WORDS]   operator, or master with his words
agentihooks swarm <id> learned                                    list every seat's learned notes with seat and number
agentihooks swarm <id> promote SEAT NUMBER insight|canon --reason TEXT   raise a learned note; canon only by master or operator
agentihooks swarm <id> retire SEAT NUMBER --reason TEXT           master or operator retires a learned note from every later prompt
agentihooks swarm <id> culture set FILE | show                    the swarm's shared culture, read by every new occupant
agent side (name from --as or AGENTIHOOKS_AGENT_NAME):
agentihooks swarm <id> issue URL | pr URL | branch | done [--pr URL] | block NOTE | handoff DOC [--recap FILE] [--reason R] | say TEXT [--to NAME|eng|ci]
agentihooks swarm <id> learned TEXT [--maturity data|note|insight|canon]   (default note; canon only by the master)
agentihooks swarm <id> exit              an agent launched with <profile> up ends its own session
agentihooks swarm <id> park DOC          hold a stacked task on its pushed branch until its open dependencies merge
agentihooks swarm <id> restack           rebase parked task work onto dev after its dependencies merge
agentihooks swarm <id> wait MINUTES [--reason TEXT]                 the tick counts no idle tick while it holds
agentihooks swarm <id> trace-plan        trace plan.md in the task work folder to task, phase and project intent
done carries the proof its task's kind needs: ops and tune --command C --output O; troubleshoot --root-cause R
--evidence E with --fix URL or --filed FOLLOWUP; research --finding URL
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from hooks.context import injection_trace, quarantine
from scripts.doctor import priming
from scripts.gates import Who, catalog, intent, modes, progress, quiet
from scripts.gates import log as gate_log
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
    affinity,
    agent_up,
    bottleneck,
    clearance,
    control_notifications,
    delivery,
    dev_red,
    dispatch_seat,
    dispatcher,
    done_gate,
    idle,
    launch_check,
    ledger_events,
    ledger_probe,
    ledger_watchdog,
    master_launch,
    merge_queue,
    metrics,
    naming,
    overlays,
    phase_planning,
    phase_state,
    phases,
    plan_review,
    priming_trace,
    prompt,
    reaper,
    snapshot,
    stack,
    take_master,
    templates,
    tick_master,
    timer,
    timing,
    trace_plan,
    waits,
)
from scripts.swarm.health import activity
from scripts.swarm.health import findings as health
from scripts.swarm.ledger_client import LedgerClient, LedgerGone, LedgerRefused
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.status import auto_snapshot, findings, status_report, task_counts, verdict_store
from scripts.swarm.store import (
    ASSIST,
    AUTO_SCALING,
    AUTONOMY,
    DELEGATE,
    DISPATCH,
    MASTER,
    SwarmConfig,
    SwarmError,
    connect,
)
from scripts.swarm.tick import agent_status, primed, skip_refused, spawn_holds, tick
from scripts.swarm_ledger import ledger_creator, ledger_kinds, ledger_link, ledger_workspace, plan_shape
from scripts.swarm_v2 import masters
from scripts.swarm_v2.runtime.routed import routed

SETTABLE = {
    "max-eng-agents": "max_eng",
    "max-ci-agents": "max_ci",
    "max-plan-agents": "max_plan",
    "compact-limit": "compact_limit",
    "snapshot-minutes": "snapshot_minutes",
}
LANE_KEYS = {f"{lane}-{key}": (lane, key) for lane in templates.LANES for key in templates.LANE_FIELDS}
EFFORT_KEYS = {"effort-min": "effort_min", "effort-max": "effort_max"}
SCALING_KEYS = {
    "scaling": "scaling",
    "load-high": "load_high",
    "load-low": "load_low",
    "memory-per-agent": "memory_per_agent_mb",
}
GATE_KEYS = {f"{name}-gate": name for name in catalog.defaults()}
GATE_MODES = modes.MODES
RETIRES_MASTER = frozenset({"stop now", "close ledger"})
TICK_LOCK_MS = 10 * 60 * 1000
TICK_SECONDS = 60
# systemd stops a pass at TimeoutStartSec=540; leave an extra tick room to finish.
EXTRA_TICKS_UNTIL = 420
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ONLY_MASTER_CANON = "only the master or the operator makes a learned note canon"
ONLY_MASTER_RETIRE = "only the master or the operator retires a learned note"
CLOSE_ASK = (
    "The operator pressed Close on the ledger page. Write one short paragraph in plain words on where the work "
    'stands, then run: agentihooks swarm {slug} close --note "<your paragraph>". Close retires you too.'
)


def now_ms():
    return int(time.time() * 1000)


@timing.instrument_tick
def run_tick(store, slug, ledger=None, runtime=None, messenger=None, scheduled=False):
    from scripts.swarm import command_runner, commands, controller, incidents, lease

    ledger = ledger or LedgerClient()
    held = lease.acquire(store, slug, commands.hive_id())
    if held is None:
        return ["the swarm belongs to another hive"]
    token = controller.take_tick_lock(store, slug, held, TICK_LOCK_MS)
    if token is None:
        return ["another tick is running"]
    keeping = timing.BEFORE_STEP.set(lambda: controller.keep_tick(store, slug, held, token, TICK_LOCK_MS))
    try:
        ledger = controller.FencedLedger(store, slug, held, ledger)
        runtime = controller.FencedRuntime(
            store,
            slug,
            held,
            runtime or routed(herdr=HerdrRuntime()),
            os.environ.get("AGENTIHOOKS_DEPLOYMENT", "local") == "local",
        )
        from scripts.swarm.health import spawn_stall

        pressured = skip_refused(incidents.host_pressure, store, slug)
        restarted = pressured + timing.call(ledger_watchdog.watch, store, slug, ledger, runtime)
        probed = restarted + timing.call(ledger_probe.observe, store, slug, ledger, runtime, now_ms())
        with spawn_stall.watch(store, slug, ledger, now_ms, runtime):
            controls = timing.call(command_runner.consume, store, slug)
            if timing.call(ledger.binned, slug):
                _, left = stop_now(store, slug, runtime or routed(herdr=HerdrRuntime()), ledger)
                return [
                    f"the ledger is in the bin, still retiring {', '.join(left)}"
                    if left
                    else "the ledger is in the bin, stopped"
                ]
            inbox = InboxStore(store.redis)
            try:
                doc = timing.call(ledger.state, slug)
            except LedgerGone as exc:
                first = store.redis.set(store.key(slug, "ledger-gone"), 1, nx=True)
                return (
                    [f"{exc}; agentihooks swarm remove {slug} clears this swarm once it has no agents"] if first else []
                )
            actions = skip_refused(phase_planning.planning_pass, inbox, store, slug, doc, ledger, store.config(slug))
            if actions:
                doc = timing.call(ledger.state, slug)
            ticked = skip_refused(phases.phase_pass, inbox, store, slug, doc, ledger)
            actions += ticked
            if ticked:
                actions += skip_refused(
                    phase_planning.planning_pass,
                    inbox,
                    store,
                    slug,
                    timing.call(ledger.state, slug),
                    ledger,
                    store.config(slug),
                )
            actions += timing.call(tick, slug, store, ledger, runtime or routed(herdr=HerdrRuntime()), now_ms())
            if store.config(slug).template == "doctor":
                from scripts.doctor import cli as doctor

                actions += skip_refused(doctor.timer, store, slug, now_ms(), scheduled)
            herdr = messenger or delivery.HerdrMessenger()
            timing.call(delivery.migrate_outbox, store, slug, inbox)
            agents = [a for a in timing.call(store.agents, slug) if a.state != "finished"]
            skip_refused(delivery.relay_to_page, inbox, slug, agents, ledger)
            doc, config = timing.call(ledger.state, slug), store.config(slug)
            view = timing.call(ledger_events.tick_view, inbox, store, slug, doc)
            actions += skip_refused(ledger_events.event_pass, inbox, store, slug, doc, ledger, now_ms(), view)
            actions += skip_refused(done_gate.recheck_pass, store, slug, doc, ledger, now_ms(), view)
            mail, mode = ledger_events.Mail(inbox, store, slug), intent.mode_of(config)
            actions += skip_refused(
                intent.Check(
                    slug,
                    mode,
                    now_ms(),
                    ledger,
                    mail,
                    intent.pr_view,
                    intent.judge,
                    head=lambda url: getattr(view(url), "head", None),
                ).run,
                doc,
            )
            actions += skip_refused(progress.checks_pass, store.redis, slug, doc["tasks"], view, now_ms())
            rows = {t["id"]: t for t in doc["tasks"]}
            actions += skip_refused(waits.end_pass, store, slug, rows, inbox, view, now_ms(), ledger_events.view)
            actions += skip_refused(quiet.quiet_pass, store, slug, rows, now_ms())
            actions += skip_refused(dispatcher.priorities, store, slug, doc, ledger, view, now_ms())
            found = timing.call(
                findings, store, slug, config, doc.get("tasks", []), doc.get("_meta", {}).get("events", [])
            )
            actions += skip_refused(ledger_events.findings_pass, inbox, store, slug, found)
            actions += timing.call(
                metrics.record_pass,
                slug,
                now_ms(),
                len(actions),
                os.environ,
                metrics.metrics_swarm.TickInput(store, doc, found, view, ledger),
            )
            window = wake.window_ms(os.environ)
            actions += skip_refused(
                wake.wake_pass, inbox, slug, agents, herdr, ledger, now_ms(), window, wake.quiet_ms(os.environ)
            )
            taken = timing.call(snapshot.auto, store, slug, now_ms(), os.environ)
            store.redis.set(store.key(slug, "last-tick"), now_ms())
            timing.call(command_runner.publish, store, slug, timing.call(ledger.state, slug))
            return probed + controls + actions + ([f"took automatic snapshot {taken.name}"] if taken else [])
    finally:
        timing.BEFORE_STEP.reset(keeping)
        controller.release_tick_lock(store, slug, token)


def cmd_list(store, args):
    for slug in store.slugs():
        c = store.config(slug)
        print(
            f"{naming.swarm_name(c.code) or '-'}\t{slug}\t{c.state}\teng {c.max_eng}\tci {c.max_ci}\tplan {c.max_plan}\tscaling {c.scaling}\tagents {len(store.agents(slug))}\t{c.repo}"
        )


def _tick_one(store, slug):
    try:
        for action in run_tick(store, slug, scheduled=True):
            timing.emit(sys.stdout, f"{slug}: {action}")
    except Exception as exc:
        timing.emit(sys.stderr, f"{slug}: {type(exc).__name__}: {exc}")


class _Firsts:
    def __init__(self, count):
        self.left, self.lock, self.settled = count, threading.Lock(), threading.Event()

    def done(self):
        with self.lock:
            self.left -= 1
            if not self.left:
                self.settled.set()


def _tick_while_others_run(store, slug, firsts, until):
    started = time.monotonic()
    try:
        _tick_one(store, slug)
    finally:
        firsts.done()
    while not firsts.settled.wait(max(0.0, started + TICK_SECONDS - time.monotonic())):
        started = time.monotonic()
        if started > until:
            return
        _tick_one(store, slug)


def cmd_tick(store, args):
    from scripts import operator_env

    if why := timer.installed_refusal():
        raise SwarmError(f"the tick refused to run: {why}")
    operator_env.fill(os.environ)
    if os.environ.get("AGENTIHOOKS_CONTROLLER_TICK_SECONDS"):
        raise SwarmError(
            "the tick refused to run: AGENTIHOOKS_CONTROLLER_TICK_SECONDS is for controller installs, "
            "and the host timer ticks every 60 seconds"
        )
    slugs = store.slugs()
    if slugs:
        firsts, until = _Firsts(len(slugs)), time.monotonic() + EXTRA_TICKS_UNTIL
        with ThreadPoolExecutor(max_workers=len(slugs)) as pool:
            list(pool.map(lambda slug: _tick_while_others_run(store, slug, firsts, until), slugs))
    from scripts import herdr_gc

    try:
        for line in herdr_gc.run(dict(os.environ), now_ms(), True):
            print(f"herdr: {line}")
    except Exception as exc:
        print(f"herdr: {type(exc).__name__}: {exc}", file=sys.stderr)


def cmd_controller(store, args):
    from dataclasses import asdict

    from scripts.swarm import commands, lease

    store.config(args.slug)
    held = lease.current(store, args.slug)
    if args.action == "release":
        released = bool(held and held.owner == commands.hive_id() and lease.release(store, args.slug, held))
        print(json.dumps({"released": released}))
    else:
        print(json.dumps(asdict(held) if held else {"owner": "", "epoch": 0, "expires_at": 0}))


def cmd_waker(store, args):
    from scripts.inbox import waker

    waker.run(store, delivery.HerdrMessenger(), now_ms)


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
    from scripts.swarm import commands

    commands.bind(store, args.slug, commands.hive_id())
    print(json.dumps({"created": args.slug, "repo": repo, "state": "paused", "template": args.template}))
    print(ledger_link.page_line(args.slug))


def _state(store, args, state):
    if state == "running":
        store.redis.delete(store.key(args.slug, "master-retired-tasks"))
    store.update(args.slug, state=state)
    if state == "running" and not timer.ensure(timer.entry_point()):
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
    config, left = stop_now(store, args.slug, routed(herdr=HerdrRuntime()), LedgerClient())
    print(json.dumps({"swarm": args.slug, "state": config.state, "still_running": left}))


def stop_now(store, slug, runtime, ledger):
    store.update(slug, state="stopping")
    rows = {t["id"]: t for t in ledger.tasks(slug)}
    left = []
    for agent in store.agents(slug):
        if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
            left.append(agent.name)
            continue
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)
        row = rows.get(agent.task, {})
        if agent.state != "finished" and row.get("state") in ("claimed", "pr") and row.get("claimed_by") == agent.name:
            try:
                ledger.update_task(slug, agent.task, {"state": "open", "claimed_by": ""})
            except LedgerRefused as exc:
                print(f"task {agent.task} not reopened, the ledger refused its write: {exc}", file=sys.stderr)
    config = store.update(slug, state="stopping" if left else "stopped")
    if not left:
        runtime.close_space(config)
    return config, left


def _live_master(store, slug, live):
    return next((a for a in store.agents(slug) if a.lane == MASTER and a.state != "finished" and a.name in live), None)


def _retire_each(store, slug, runtime, agents):
    left = []
    for agent in agents:
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)
        if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
            store.put_agent(slug, agent)
            left.append(agent.name)
    return left


def cmd_close(store, args):
    store.config(args.slug)
    runtime = routed(herdr=HerdrRuntime())
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
    left = _retire_each(store, args.slug, runtime, [a for a in agents if a.lane != MASTER])
    for row in ledger.tasks(args.slug):
        if row.get("state") == "claimed":
            ledger.update_task(args.slug, row["id"], {"state": "open", "claimed_by": ""})
    ledger.mark_closed(args.slug, by)
    store.update(args.slug, state="stopping" if left else "stopped")
    print(json.dumps({"closed": args.slug, "snapshot": str(path), "still_running": left}), flush=True)
    masters_left = _retire_each(store, args.slug, runtime, [a for a in agents if a.lane == MASTER])
    if masters_left:
        store.update(args.slug, state="stopping")
    elif not left:
        exits.close_swarm(InboxStore(store.redis), args.slug)


def cmd_reopen(store, args):
    runtime = routed(herdr=HerdrRuntime())
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
    runtime = routed(herdr=HerdrRuntime())
    if args.slug not in store.slugs():
        snapshot.recreate(store, args.slug, runtime.live_names())
    from scripts.gates import Who

    name = Who.from_env().name
    record, transfer = take_master.take(store, args.slug, name, runtime, now_ms(), args.replace)
    ledger = LedgerClient()
    if ledger.closed(args.slug):
        ledger.reopen(args.slug, record.name)
    if store.config(args.slug).state in ("stopped", "stopping"):
        store.update(args.slug, state="running")
        timer.ensure(timer.entry_point())
    config = store.config(args.slug)
    task = {
        "id": MASTER,
        "handoff": store.handoff(args.slug, MASTER),
        "peer": store.peer(args.slug),
        "transfer": transfer,
    }
    task = primed(store, args.slug, record.seat, task)
    print(prompt.build_master(args.slug, config.repo, record.name, task, config.autonomy))
    injection_trace.record_rows(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), priming_trace.rows(args.slug, task))
    store.clear_handoff(args.slug, MASTER)


def cmd_master(store, args):
    runtime = routed(herdr=HerdrRuntime())
    if args.slug not in store.slugs():
        snapshot.recreate(store, args.slug, runtime.live_names())
    launched = master_launch.up(store, args.slug, runtime, now_ms(), args.choice, input, print)
    ledger = LedgerClient(service=True)
    if ledger.closed(args.slug):
        ledger.reopen(args.slug, launched.master)
    if store.config(args.slug).state in ("stopped", "stopping"):
        store.update(args.slug, state="paused")
        timer.ensure(timer.entry_point())
    ledger.join(args.slug, launched.master, "orchestrator")
    print(json.dumps(asdict(launched)))


def cmd_agent_up(store, args):
    launched = agent_up.up(store, args.slug, HerdrRuntime(), args.profile, now_ms())
    print(json.dumps(asdict(launched)))


def cmd_exit(store, args):
    name = store.names.resolve(args.name or Who.from_env().name)
    agent_up.retire(store, args.slug, name, now_ms())
    print(json.dumps({"exited": name}), flush=True)
    HerdrRuntime().reap_name(name)


def gate_mode(key, value):
    value = modes.normalize(value)
    if value not in modes.supported(GATE_KEYS[key]):
        raise SwarmError(f"{key} takes {', '.join(modes.label(mode) for mode in modes.supported(GATE_KEYS[key]))}")
    return {GATE_KEYS[key]: value}


def setting(config, key):
    if key in GATE_KEYS:
        return modes.label(catalog.current(config.gates)[GATE_KEYS[key]])
    if key in LANE_KEYS:
        lane, field = LANE_KEYS[key]
        return config.lanes.get(lane, {}).get(field) or "unset"
    return getattr(config, {**SETTABLE, **EFFORT_KEYS, **SCALING_KEYS}.get(key, key))


def lifecycle_control(args):
    if args.command == "close":
        return "close ledger"
    return "stop now" if vars(args).get("now") else args.command


def control_readings(store, args):
    from scripts.gates import lift

    config = store.config(args.slug)
    if args.command == "set":
        keys = (pair.partition("=")[0] for pair in args.pairs)
        return {
            key: setting(config, key)
            for key in keys
            if key in (*SETTABLE, *LANE_KEYS, *EFFORT_KEYS, *SCALING_KEYS, *GATE_KEYS, "autonomy")
        }
    if args.command == "lift":
        lifted = lift.agent_lifted(args.slug, args.agent, args.gate)
        return {f"{args.gate} gate lift for {args.agent}": "lifted" if lifted else "not lifted"}
    return {f"the swarm state with {lifecycle_control(args)}": config.state}


def run_control(store, args, who, handler):
    cleared = clearance.holder(store, args.slug, who)
    if cleared == clearance.OPERATOR:
        return handler(store, args)
    before = control_readings(store, args)
    if lifecycle_control(args) in RETIRES_MASTER:
        stopping = dict.fromkeys(before, "stopping")
        clearance.record(LedgerClient(), args.slug, cleared, before, stopping)
        before = stopping
    handler(store, args)
    after = control_readings(store, args)
    if after != before or lifecycle_control(args) not in RETIRES_MASTER:
        clearance.record(LedgerClient(), args.slug, cleared, before, after)


def scaling_value(key, value):
    if key == "scaling":
        return value
    if key == "memory-per-agent":
        if not value.isdigit():
            raise SwarmError("memory-per-agent takes a whole number of MB")
        return int(value)
    try:
        return float(value)
    except ValueError:
        raise SwarmError(f"{key} takes a number, the one minute load per CPU") from None


def _master_counts(pairs):
    """The master seat count lives beside the swarm config: checked with the other pairs, stored after them."""
    return [masters.count_of(pair.partition("=")[2]) for pair in pairs if pair.partition("=")[0] == "masters"]


def _store_master_counts(store, slug, counts):
    for count in counts:
        masters.MasterSeats(store.redis).set_count(slug, count)


def cmd_set(store, args):
    changes, lanes = {}, {key: dict(value) for key, value in store.config(args.slug).lanes.items()}
    counts = _master_counts(args.pairs)
    for pair in [pair for pair in args.pairs if pair.partition("=")[0] != "masters"]:
        key, _, value = pair.partition("=")
        if key in LANE_KEYS:
            lane, field = LANE_KEYS[key]
            lanes.setdefault(lane, {})[field] = value
            changes["lanes"] = templates.lane_map(templates.parse({"name": "set", "lanes": lanes}))
            continue
        if key == "autonomy":
            changes["autonomy"] = value
            continue
        if key in EFFORT_KEYS:
            changes[EFFORT_KEYS[key]] = value
            continue
        if key in SCALING_KEYS:
            changes[SCALING_KEYS[key]] = scaling_value(key, value)
            continue
        if key in GATE_KEYS:
            changes["gates"] = {**store.config(args.slug).gates, **gate_mode(key, value)}
            continue
        if key.startswith(overlays.KEY):
            try:
                changes["overlays"] = overlays.setting(
                    key, value, changes.get("overlays", store.config(args.slug).overlays), overlays.available()
                )
            except ValueError as exc:
                raise SwarmError(str(exc)) from exc
            continue
        if key not in SETTABLE or not value.isdigit():
            raise SwarmError(
                f"set takes {', '.join(SETTABLE)}=<whole number>, autonomy={'|'.join(AUTONOMY)}, masters=N, "
                f"effort-min=E, effort-max=E, scaling=auto|manual, load-high=N, load-low=N, memory-per-agent=MB "
                f"or {', '.join(LANE_KEYS)}=<value>"
            )
        changes[SETTABLE[key]] = int(value)
    config = store.update(args.slug, **changes)
    _store_master_counts(store, args.slug, counts)
    asked = any(pair.startswith("master-agent=") for pair in args.pairs)
    master = affinity.order(store, args.slug, now_ms()) if asked else affinity.pending(store, args.slug)
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
                "snapshot_minutes": config.snapshot_minutes,
                "effort_min": config.effort_min,
                "effort_max": config.effort_max,
                "lanes": config.lanes,
                "overlays": config.overlays,
                "scaling": config.scaling,
                "load_high": config.load_high,
                "load_low": config.load_low,
                "memory_per_agent_mb": config.memory_per_agent_mb,
                "master_affinity": {"desired": affinity.desired(config) or "auto", "order": master},
                "masters": masters.MasterSeats(store.redis).count(args.slug),
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
    outcomes = snapshot.restore(store, args.slug, herdr.live_names(), source, runtime=routed(herdr=herdr))
    for action in run_tick(store, args.slug):
        print(action)
    state = store.config(args.slug).state
    restored = [asdict(o) for o in outcomes]
    print(json.dumps({"swarm": args.slug, "state": state, "snapshot": str(source), "restored": restored}))


def _snapshot_line(auto):
    every = f"every {auto['every_minutes']} min" if auto["every_minutes"] > 0 else "automatic snapshots off"
    if auto["last"] is None:
        return f"snapshots  no automatic snapshot yet  {every}"
    taken = datetime.fromtimestamp(auto["last"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"snapshots  last automatic snapshot {taken}  {every}  kept {auto['kept']}"


def _affinity_line(report):
    line = f"master affinity  desired {report['desired']}  live {report['live'] or 'none'}"
    order = report["order"]
    if order is None:
        return line
    reason = f": {order['reason']}" if order["reason"] else ""
    return f"{line}  order to {order['to']} {order['state']}{reason}"


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
        f"{naming.swarm_name(config.code) or '-'}  {config.slug}  {config.state}  eng {config.max_eng}  ci {config.max_ci}  plan {config.max_plan}  scaling {config.scaling}  effort {config.effort_min} to {config.effort_max}  repo {config.repo}"
    )
    print(
        "gate modes  "
        + "  ".join(f"{name}={modes.label(mode)}" for name, mode in catalog.current(config.gates).items())
    )
    print("tasks  " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    print(plan_shape.report(tasks, config.max_eng)["summary"])
    for phase_id, state, held in phase_state.report(doc):
        print(f"phase {phase_id}  {state}" + (f"  holds {', '.join(held)}" if held else ""))
    from scripts.swarm import capacity, quota_view
    from scripts.swarm_ledger import ledger_freezes

    quota = quota_view.lines(capacity.read(store, args.slug), now_ms())
    for line in ledger_freezes.lines(doc) + quota + spawn_holds(store, args.slug):
        print(line)
    print(bottleneck.line(bottleneck.read(store, args.slug), now_ms()))
    print(_snapshot_line(auto_snapshot(config)))
    print(_affinity_line(affinity.report(store, args.slug, config, agents)))
    if promotion := tick_master.status_line(tick_master.read(store, args.slug)):
        print(promotion)
    print("Agent\tLane\tHarness\tProfile\tModel\tAccount\tPane\tTask\tState\tConversation\tModel source\tConfidence")
    for a in agents:
        model = " ".join(filter(None, (a.model, a.effort))) if a.model else "unknown"
        print(
            f"{a.name}\t{a.lane}\t{a.harness}\t{a.profile or 'unknown'}\t{model}\t{a.account or '-'}\t{a.pane_id}\t{a.task}\t{agent_status(a)}\t{a.conversation_id or '-'}\t{a.model_source or '-'}\t{a.model_confidence if a.model_confidence is not None else '-'}"
        )
    for r in store.restored(args.slug):
        print(f"restored  {r['name']}  {r['outcome']}  {r['reason']}")
    for launch in launch_check.reports(store, args.slug):
        print(f"launch  {launch['agent']}  {launch['state']}  {launch['elapsed_ms']}ms")
        for field, values in launch["misses"].items():
            mode = "report only" if field in launch_check.REPORT_ONLY else "enforced"
            print(f"  {field}  {mode}  expected {values['expected']}; observed {values['actual']}")
    for f in found:
        print(f"finding  {f['kind']}  {f['subject']}: {f['summary']}")
        for entry in f["evidence"]:
            print(f"  - {entry}")
        print(f"  threshold {f['threshold']}")
        print(f"  id {f['id']}" + (f"  earlier verdict {f['verdict']['value']}" if f["verdict"] else ""))
    for row in gate_log.decisions(args.slug):
        print(
            f"gate  {modes.label(row['kind'])}  {row.get('gate')}  {row.get('agent')}  {row.get('task')}  {row.get('reason')}"
        )


def cmd_freeze(store, args):
    writer = clearance.freezer(store, args.slug, Who.from_env())
    by = None if writer == clearance.OPERATOR else writer
    if by and naming.lane_of(by) != DISPATCH and not args.quote:
        raise SwarmError("the master writes freezes only with the operator's words: pass them with --quote")
    LedgerClient().freeze(args.slug, args.command, args.target, by=by, reason=args.reason, quote=args.quote)
    print(json.dumps({args.command: args.target, "by": writer}))


cmd_focus = cmd_unfreeze = cmd_freeze


def autoscale_lines(config, decision):
    caps = decision["ceilings"]
    pending = decision["pending_raise"]
    lines = [f"scaling {config.scaling}, ceilings eng {caps['eng']}, ci {caps['ci']}, plan {caps['plan']}"]
    if config.scaling != AUTO_SCALING:
        lines.append(
            f"manual scaling keeps the configured caps eng {config.max_eng}, ci {config.max_ci}, plan {config.max_plan}"
        )
    if pending["target"] is not None:
        lines.append(f"pending raise to {pending['target']}, held {pending['ticks']} of 3 ticks")
    lines.append(f"host room {decision['host']['room']}: {decision['host']['reason']}")
    lines.append(decision["reason"])
    return lines


def cmd_autoscale(store, args):
    from scripts.swarm import capacity

    config = store.config(args.slug) if args.slug in store.slugs() else SwarmConfig(args.slug, "", max_eng=0, max_ci=0)
    if args.fixture:
        inputs = capacity.fixture_inputs(json.loads(Path(args.fixture).read_text()))
    else:
        inputs = capacity.live_inputs(args.slug, store, LedgerClient(), dict(os.environ), now_ms())
    _, decision = capacity.autoscaled(replace(config, scaling=AUTO_SCALING), inputs)
    if args.json:
        print(json.dumps({"scaling": config.scaling, "applied": config.scaling == AUTO_SCALING, **decision}, indent=2))
        return
    print("\n".join(autoscale_lines(config, decision)))


def cmd_names(store, args):
    config = store.ensure_code(args.slug)
    rows = store.names.names(args.slug)
    if args.json:
        print(
            json.dumps(
                {
                    "name": naming.swarm_name(config.code),
                    "code": config.code,
                    "space": naming.space(config.repo, config.code, config.slug),
                    "names": rows,
                }
            )
        )
        return
    print(
        f"name {naming.swarm_name(config.code)}\tcode {config.code}\tspace {naming.space(config.repo, config.code, config.slug)}"
    )
    for row in rows:
        retired = row["retired_at"] or "-"
        print(
            f"{row['name']}\t{row['type']}\t{row['number']}\t{row['session_id'] or '-'}\t{row['spawned_at']}\t{retired}"
        )


def cmd_rename(store, args):
    from scripts.herdr_host import HerdrError
    from scripts.swarm.rename import rename_swarm

    slugs = [args.slug] if getattr(args, "slug", "") else store.slugs()
    ledger, runtime, failed = LedgerClient(), routed(herdr=HerdrRuntime()), []
    for slug in slugs:
        store.ensure_code(slug)
        if not store.agents(slug):
            continue
        try:
            for action in rename_swarm(store, slug, ledger, runtime, now_ms()):
                print(f"{slug}: {action}")
        except (SwarmError, HerdrError) as exc:
            failed.append(f"{slug}: {exc}")
    if failed:
        raise SwarmError("; ".join(failed))


def _master_or_operator(store, args, message):
    store.config(args.slug)
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    if name in ("", "operator"):
        return "operator"
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent is None or agent.lane != MASTER:
        raise SwarmError(message)
    return name


def cmd_verdict(store, args):
    by = _master_or_operator(store, args, "only the master or the operator gives a finding a verdict")
    verdict = verdict_store(store, args.slug).judge(args.finding, args.verdict, args.note, by, now_ms())
    minutes = health.limits().cooldown_minutes
    print(json.dumps({"finding": args.finding, "verdict": verdict["value"], "hidden_minutes": minutes}))


def cmd_classify(store, args):
    from scripts.swarm_v2.runtime import observe

    by = _master_or_operator(store, args, "only the master or the operator classifies an execution attempt")
    try:
        ruled = observe.rule(store, args.slug, args.execution_id, args.ruling, args.reason, by, time.time())
    except observe.ObservationRefused as exc:
        raise SwarmError(str(exc)) from exc
    print(
        json.dumps(
            {
                "execution_id": ruled.execution_id,
                "state": ruled.state.value,
                "ruling": ruled.ruling,
                "reason": ruled.ruling_reason,
                "by": ruled.ruled_by,
            }
        )
    )


def cmd_lift(store, args):
    from scripts.gates import entry, lift

    store.config(args.slug)
    agent = next((a for a in store.agents(args.slug) if a.name == args.agent), None)
    if agent is None:
        raise SwarmError(f"{args.agent} is not in this swarm")
    if args.gate not in {*entry.GATES, *lift.SERVER_GATES}:
        raise SwarmError(f"no gate named {args.gate}")
    lift.lift_agent(Who(name=agent.name, swarm=args.slug, task=agent.task), args.gate)
    print(json.dumps({"agent": agent.name, "gate": args.gate, "minutes": lift.LIFT_SECONDS // 60}))


def cmd_send_message(store, args):
    store.config(args.slug)
    sender = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME") or delivery.OPERATOR
    print(json.dumps({"sent": delivery.send(store, args.slug, args.text, sender=sender, to="all")}))


def _me(store, args):
    from scripts.gates import Who

    name = store.names.resolve(args.name or Who.from_env().name)
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


def _git(run, *args):
    try:
        return run(["git", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SwarmError(f"git {args[0]} could not run: {exc}") from exc


def worktree_branch(run=subprocess.run):
    current = _git(run, "branch", "--show-current")
    branch = current.stdout.strip() if current.returncode == 0 else ""
    if not branch:
        raise SwarmError("swarm branch runs in a worktree on a branch; this checkout is on none")
    if _git(run, "ls-remote", "--exit-code", "--heads", "origin", branch).returncode != 0:
        raise SwarmError(f"branch {branch} is not on origin; push it first with git push -u origin {branch}")
    return branch


PULL_HEAD = "headRefName,headRepository,headRepositoryOwner"


def pull_branch(url, run=subprocess.run):
    try:
        done = run(["gh", "pr", "view", url, "--json", PULL_HEAD], capture_output=True, text=True, timeout=20)
        found = json.loads(done.stdout) if done.returncode == 0 else {}
        return found["headRefName"], _head_repo(url, found)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return "", ""


def _head_repo(url, found):
    owner, repo = found.get("headRepositoryOwner") or {}, found.get("headRepository") or {}
    if not (owner.get("login") and repo.get("name")):
        return ""
    return f"{url.split('/pull/')[0].rsplit('/', 2)[0]}/{owner['login']}/{repo['name']}"


def origin_repo(run=subprocess.run):
    done = _git(run, "remote", "get-url", "origin")
    return stack.public_url(done.stdout.strip()) if done.returncode == 0 else ""


def cmd_branch(store, args):
    agent = _worker(store, args)
    branch = worktree_branch()
    fields = {"branch": branch, "branch_repo": origin_repo()}
    LedgerClient().update_task(args.slug, agent.task, fields, by=agent.name)
    print(json.dumps({"task": agent.task, **fields}))


def cmd_pr(store, args):
    agent = _worker(store, args)
    config = store.config(args.slug)
    awaiting = "approval" if config.autonomy == ASSIST else ""
    head, repo = pull_branch(args.url)
    branch = {"branch": head, "branch_repo": repo} if head else {}
    fields = {"pr_url": args.url, "state": "pr", "awaiting": awaiting, **branch}
    ledger = LedgerClient()
    checked = intent.stamp(args.slug, agent.task, args.url, ledger.state(args.slug), intent.mode_of(config), now_ms())
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    print(json.dumps({"task": agent.task, "pr_url": args.url, **branch, "intent": checked}))


def cmd_merge(store, args):
    agent = _worker(store, args) if args.action != "state" else None
    if agent is not None:
        done_gate.require_local(store, args.slug, agent.task)
        done_gate.require_target(store, args.slug, agent.task, args.url, LedgerClient().tasks(args.slug))
    result = merge_queue.operate(args.action, args.url)
    if result.get("waiting") == "checks":
        at = now_ms()
        idle.declare_wait(
            store.redis,
            args.slug,
            agent.name,
            at + waits.CHECKED_MINUTES * 60_000,
            "dev changed grading inputs; branch updated",
            at,
            on={**waits.on("checks", args.url), "previous_head": result["previous_head"]},
        )
    print(json.dumps(result))


def cmd_done(store, args):
    agent = _worker(store, args)
    if agent.lane == dispatch_seat.LANE:
        return _dispatcher_done(store, args.slug, agent)
    done_gate.require_local(store, args.slug, agent.task)
    ledger = LedgerClient()
    row = next((t for t in ledger.tasks(args.slug) if t.get("id") == agent.task), {})
    proof = {key: getattr(args, f"proof_{key}") for key in ledger_kinds.PROOF_KEYS if getattr(args, f"proof_{key}")}
    missing = ledger_kinds.unmet({**row, "proof": {**(row.get("proof") or {}), **proof}})
    if missing:
        if ledger_kinds.kind(row) == "research":
            raise SwarmError(
                "a research task is done only with its proof: "
                "--finding must be a single link to the artifact; put prose in a task comment"
            )
        flags = ", ".join("--" + key.replace("_", "-").replace(" or ", " or --") for key in missing)
        raise SwarmError(f"a {ledger_kinds.kind(row)} task is done only with its proof: give {flags}")
    url = args.pr or row.get("pr_url")
    pull = ledger_events.view(url) if ledger_kinds.kind(row) in done_gate.GATED and url else None
    if pull is not None and pull.state == "OPEN" and pull.queued:
        at = now_ms()
        idle.declare_wait(
            store.redis,
            args.slug,
            agent.name,
            at + waits.CHECKED_MINUTES * 60_000,
            "merge queue",
            at,
            on=waits.on("merge", url),
        )
        raise SwarmError(f"pull request {url} is in the merge queue; waiting for it to land before swarm done")
    refused = done_gate.refusal(row, url, lambda target: pull)
    if refused:
        raise SwarmError(refused)
    fields = {"state": "done", **({"pr_url": args.pr} if args.pr else {}), **({"proof": proof} if proof else {})}
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    _close_members(ledger, args.slug, agent, row, {**fields, "pr_url": args.pr or row.get("pr_url")})
    waits.settle_notices(InboxStore(store.redis), agent, "done")
    _retire(store, args.slug, agent, "finished its task and exited")
    print(json.dumps({"task": agent.task, "state": "done", "next": "stop now; the swarm closes this session"}))


def _dispatcher_done(store, slug, agent):
    refused = dispatch_seat.refusal(slug, store.config(slug), store, LedgerClient().state(slug), now_ms())
    if refused:
        raise SwarmError(refused)
    _retire(store, slug, agent, "settled its triggers and exited")
    print(json.dumps({"seat": agent.name, "state": "finished", "next": "stop now; the swarm closes this session"}))


def _close_members(ledger, slug, agent, lead, fields):
    members = set(lead.get("group_members") or [])
    for task in ledger.tasks(slug):
        if task["id"] in members and task["state"] != "done":
            ledger.update_task(slug, task["id"], fields, by=agent.name)


def cmd_block(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    rows = {task["id"]: task for task in ledger.tasks(args.slug)}
    for dependency in rows[agent.task].get("depends_on", []):
        if rows[dependency]["state"] != "done":
            held = waits.on("task", dependency)
            at = now_ms()
            until = at + waits.CHECKED_MINUTES * 60_000
            idle.declare_wait(store.redis, args.slug, agent.name, until, args.note, at, on=held)
            waits.settle_notices(InboxStore(store.redis), agent, "a new wait")
            print(json.dumps({"task": agent.task, "state": "claimed", "waits_on": held}))
            return
    red_run = _dev_red_run(store, args.slug) if args.dev_red else None
    block_agent(store, args.slug, agent, args.note, ledger, red_run)
    cause = {"dev_red_run": red_run} if red_run is not None else {}
    print(
        json.dumps({"task": agent.task, "state": "blocked", **cause, "next": "stop now; the swarm closes this session"})
    )


def _dev_red_run(store, slug):
    try:
        red_run = dev_red.failing_run(store.config(slug).repo)
    except dev_red.READ_ERRORS as exc:
        raise SwarmError(f"cannot read the dev Tests runs: {getattr(exc, 'stderr', None) or exc}") from exc
    if red_run is None:
        raise SwarmError(
            "the latest finished dev Tests run did not fail, so dev is not red; keep working or block for the real reason"
        )
    return red_run


def block_agent(store, slug, agent, note, ledger, red_run=None):
    dev_red.hold(store.redis, slug, agent.task, red_run)
    ledger.update_task(slug, agent.task, {"state": "blocked"}, by=agent.name)
    ledger.comment(slug, agent.task, note, by=agent.name)
    waits.settle_notices(InboxStore(store.redis), agent, "a block")
    _retire(store, slug, agent, "blocked its task and exited")


def cmd_trace_plan(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    doc = ledger.state(args.slug)
    state = trace_plan.intent(doc, agent.task)
    who = Who(name=agent.name, swarm=args.slug, task=agent.task)
    folder = ledger_workspace.folder(args.slug, agent.task)
    config = store.config(args.slug)
    mode = modes.configured(trace_plan.GATE, config.gates)
    try:
        record, block = trace_plan.run(folder, state, ledger, who, mode)
    except ValueError as exc:
        raise SwarmError(str(exc)) from exc
    if block:
        block_agent(store, args.slug, agent, trace_plan.block_note(record), ledger)
    checked = None
    if record["verdict"] != trace_plan.FAIL:
        checked = intent.plan_check(args.slug, doc, agent.task, record, intent.mode_of(config), now_ms())
    print(json.dumps({**trace_plan.report(agent.task, record, block), "intent": checked}))


def cmd_wait_inbox(store, args, agent):
    from scripts.inbox.receive import receive

    minutes = args.minutes if args.minutes is not None else 1
    if args.on or not 0 < minutes <= waits.BARE_MAX_MINUTES:
        raise SwarmError("an inbox wait takes one to sixty minutes and cannot also wait on a dependency")
    at = now_ms()
    idle.declare_wait(store.redis, args.slug, agent.name, at + minutes * 60_000, "inbox work", at)
    try:
        items = receive(InboxStore(store.redis), agent.name, minutes * 60)
        print(json.dumps({"items": [asdict(item) for item in items], "timed_out": not items}))
    finally:
        idle.end_wait(store.redis, args.slug, agent.name, now_ms())


def cmd_wait(store, args):
    agent = _me(store, args)
    if args.inbox:
        return cmd_wait_inbox(store, args, agent)
    held = waits.on(*args.on) if args.on else None
    if held is None and (args.minutes or 0) <= 0:
        raise SwarmError("a wait lasts a whole number of minutes above zero")
    if held is None and args.minutes > waits.BARE_MAX_MINUTES:
        raise SwarmError(
            f"a bare wait lasts at most {waits.BARE_MAX_MINUTES} minutes; wait on checks, a reply or a task with --on"
        )
    if held is not None:
        rows = {t["id"]: t for t in LedgerClient().tasks(args.slug)}
        get = InboxStore(store.redis).get
        if problem := waits.target_problem(held["kind"], held["target"], agent.task, rows, get):
            raise SwarmError(problem)
        if held["kind"] == "checks":
            pull = ledger_events.view(held["target"])
            if pull is None or not pull.head:
                raise SwarmError("cannot read the pull request head; retry the checks wait")
            held["head"] = pull.head
        if held["kind"] == "mutation":
            held["head"] = waits.mutation_wait.bind(held["target"])
    at = now_ms()
    until = at + (args.minutes or waits.CHECKED_MINUTES) * 60_000
    idle.declare_wait(store.redis, args.slug, agent.name, until, args.reason, at, on=held)
    waits.settle_notices(InboxStore(store.redis), agent, "a new wait")
    print(
        json.dumps(
            {
                "agent": agent.name,
                "until": datetime.fromtimestamp(until / 1000, timezone.utc).isoformat(),
                **({"on": held} if held else {}),
            }
        )
    )


def cmd_progress(store, args):
    agent = _worker(store, args)
    line = quiet.report(store, LedgerClient(), args.slug, agent, quiet.Status(args.doing, args.ends_when), now_ms())
    waits.settle_notices(InboxStore(store.redis), agent, "progress")
    print(json.dumps({"agent": agent.name, "task": agent.task, "progress": line}))


def cmd_plan(store, args):
    agent, autonomy = _me(store, args), store.config(args.slug).autonomy
    decision = plan_review.Decision(args.action, args.note, args.override)
    result = plan_review.decide(LedgerClient(), args.slug, agent, autonomy, args.phase, decision)
    print(json.dumps(result))


def cmd_handoff(store, args):
    agent = _me(store, args)
    text = _read(args.doc, "handoff document")
    ledger = LedgerClient()
    found = handoff_check.problems(text, Resolver(args.slug, store.redis, ledger.state))
    if found:
        raise SwarmError(handoff_check.refusal(found))
    envelope, transfer = _hand_off(store, args.slug, agent, text, args.reason, ledger)
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


def _hand_off(store, slug, agent, text, reason, ledger):
    recap = "\n\n".join(
        f"## {heading}\n{handoff_check.section(text, heading)}" for heading in ("Done", "Stopped at", "Next")
    )
    waits.settle_notices(InboxStore(store.redis), agent, "a handoff")
    envelope = handoff_envelope.build(store, slug, agent, reason, _ledger_rows(ledger, slug), now_ms())
    store.memory.add_recap(_seat(agent), agent.name, agent.task, recap, now_ms())
    store.put_handoff(slug, agent.task, text, seat=agent.seat, envelope=envelope)
    transfer = transfers.record(store, slug, agent, reason, text, now_ms())
    store.put_agent(slug, replace(agent, state="finished"))
    exits.settle(InboxStore(store.redis), agent.name, agent.seat, "handed off its seat")
    return envelope, transfer


def cmd_park(store, args):
    agent = _worker(store, args)
    if agent.state == "finished":
        raise SwarmError(f"{agent.name} already handed off its seat; remove a leftover worktree with wt.sh done")
    text = _read(args.doc, "handoff document")
    ledger = LedgerClient()
    fields, top = stack.park(store, args.slug, agent, text, ledger)
    _hand_off(store, args.slug, agent, text, "exit", ledger)
    removed = stack.remove_worktree(top)
    print(
        json.dumps(
            {
                "task": agent.task,
                **fields,
                "worktree_removed": removed,
                "next": "stop now; the task waits on its branch",
            }
        )
    )


def cmd_restack(store, args):
    agent = _worker(store, args)
    fields = stack.restack(args.slug, agent, LedgerClient())
    print(json.dumps({"task": agent.task, **fields}))


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
    outcome = resume.decide(
        store, args.slug, args.agent, args.choice, routed(herdr=HerdrRuntime()), now_ms(), LedgerClient()
    )
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
    if refused := quarantine.patch_refusal(args.text):
        raise SwarmError(refused)
    store.memory.learn(_seat(agent), agent.name, args.text, now_ms(), args.maturity)
    print(json.dumps({"seat": agent.seat, "learned": args.text, "maturity": args.maturity}))


def _list_learned(store, slug):
    store.config(slug)
    for key in store.seats.swarm_keys(slug):
        seat, _, kind = key.removeprefix(f"{SEAT_PREFIX}:").partition(":")
        if kind != "learned":
            continue
        for number, note in enumerate(store.memory.learned(seat), 1):
            if "retired" in note:
                continue
            print(f"{seat}\t{number}\t{note['maturity']}\t{note['text']}")


def cmd_promote(store, args):
    store.config(args.slug)
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if args.maturity == CANON and agent is not None and agent.lane != MASTER:
        raise SwarmError(ONLY_MASTER_CANON)
    seat = _swarm_seat(args.slug, args.seat)
    try:
        entry = store.memory.promote(seat, args.number, args.maturity, name or "operator", args.reason, now_ms())
    except SeatError as exc:
        raise SwarmError(str(exc)) from exc
    print(json.dumps({"seat": seat, "number": args.number, "maturity": entry["maturity"]}))


def cmd_retire(store, args):
    store.config(args.slug)
    name = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME") or "operator"
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent is not None and agent.lane != MASTER:
        raise SwarmError(ONLY_MASTER_RETIRE)
    seat = _swarm_seat(args.slug, args.seat)
    try:
        store.memory.retire(seat, args.number, name, args.reason, now_ms())
    except SeatError as exc:
        raise SwarmError(str(exc)) from exc
    print(json.dumps({"seat": seat, "number": args.number, "retired": True}))


def _swarm_seat(slug, seat):
    address = seat if is_seat(seat) else seat_address(slug, seat)
    if not address.endswith(f"@{slug}"):
        raise SwarmError(f"{seat} is not a seat of swarm {slug}")
    return address


def cmd_culture(store, args):
    store.config(args.slug)
    if args.action == "set":
        text = _read(args.file, "culture file")
        if refused := quarantine.patch_refusal(text):
            raise SwarmError(refused)
        store.culture.set(args.slug, text)
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
    if args.to in ("", delivery.OPERATOR):
        LedgerClient().say(args.slug, args.text, by=agent.name)
        print(json.dumps({"posted": True}))
        return
    sent = delivery.send(store, args.slug, args.text, sender=agent.name, to=args.to, fyi=args.fyi)
    print(json.dumps({"sent": sent}))


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
    master_up = sub.add_parser("master").add_subparsers(dest="action", required=True).add_parser("up")
    pick = master_up.add_mutually_exclusive_group()
    pick.add_argument("--last", dest="choice", action="store_const", const=master_launch.LAST, default="")
    pick.add_argument("--new", dest="choice", action="store_const", const=master_launch.NEW)
    sub.add_parser("agent-up").add_argument("profile")
    sub.add_parser("exit")
    sub.add_parser("set").add_argument("pairs", nargs="+")
    sub.add_parser("save-template").add_argument("template_name", metavar="name")
    sub.add_parser("status").add_argument("--json", action="store_true")
    for verb in ("freeze", "focus", "unfreeze"):
        frozen = sub.add_parser(verb)
        frozen.add_argument("target")
        frozen.add_argument("--reason", default="")
        frozen.add_argument("--quote", default="")
    scale = sub.add_parser("autoscale")
    scale.add_argument("--fixture", default="")
    scale.add_argument("--json", action="store_true")
    sub.add_parser("controller").add_argument("action", nargs="?", choices=("release",), default=None)
    sub.add_parser("names").add_argument("--json", action="store_true")
    verdict = sub.add_parser("verdict")
    verdict.add_argument("finding")
    verdict.add_argument("verdict")
    verdict.add_argument("--note", default="")
    classify = sub.add_parser("classify")
    classify.add_argument("execution_id")
    classify.add_argument("ruling", choices=("lost", "working"))
    classify.add_argument("--reason", required=True)
    sub.add_parser("send-message").add_argument("text")
    lift = sub.add_parser("lift")
    lift.add_argument("agent")
    lift.add_argument("gate")
    for name in ("issue", "pr"):
        sub.add_parser(name).add_argument("url")
    merge = sub.add_parser("merge")
    merge.add_argument("action", choices=("queue", "dequeue", "state"))
    merge.add_argument("url")
    sub.add_parser("branch")
    done = sub.add_parser("done")
    done.add_argument("--pr", default="")
    for key in ledger_kinds.PROOF_KEYS:
        done.add_argument("--" + key.replace("_", "-"), dest=f"proof_{key}", default="")
    block = sub.add_parser("block")
    block.add_argument("note")
    block.add_argument("--dev-red", action="store_true")
    sub.add_parser("trace-plan")
    plan = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    for action in plan_review.DECISIONS:
        decision = plan.add_parser(action)
        decision.add_argument("phase")
        decision.add_argument("--note", required=action == "send-back")
        if action == "approve":
            decision.add_argument("--override", default="")
        else:
            decision.set_defaults(override="")
    wait = sub.add_parser("wait")
    wait.add_argument("minutes", type=int, nargs="?")
    wait.add_argument("--on", nargs=2, metavar=("KIND", "TARGET"))
    wait.add_argument("--reason", default="")
    wait.add_argument("--inbox", action="store_true")
    reported = sub.add_parser("progress")
    reported.add_argument("--doing", required=True)
    reported.add_argument("--ends-when", required=True)
    sub.add_parser("park").add_argument("doc")
    sub.add_parser("restack")
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
    retire = sub.add_parser("retire")
    retire.add_argument("seat")
    retire.add_argument("number", type=int)
    retire.add_argument("--reason", required=True)
    culture = sub.add_parser("culture").add_subparsers(dest="action", required=True)
    culture.add_parser("set").add_argument("file")
    culture.add_parser("show")
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--to", default="")
    say.add_argument("--fyi", action="store_true")
    return parser


def main(argv):
    if argv and argv[0] in ("list", "tick", "templates", "rename", "waker"):
        handler, args = globals()[f"cmd_{argv[0]}"], argparse.Namespace()
    else:
        if len(argv) > 1 and argv[1].partition("=")[0] in (
            *SETTABLE,
            *LANE_KEYS,
            *EFFORT_KEYS,
            *SCALING_KEYS,
            "autonomy",
        ):
            argv = [argv[0], "set", *argv[1:]]
        elif len(argv) == 3 and argv[2] == "up" and argv[1] != "master":
            argv = [argv[0], "agent-up", argv[1]]
        args = build_parser().parse_args(argv)
        handler = globals()[f"cmd_{args.command.replace('-', '_')}"]
    who = Who.from_env()
    if text := refusal(vars(args).get("name"), who):
        print(f"swarm: {text}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        store = connect()
        if "slug" in args:
            args.slug = store.names.swarm_slug(args.slug)
        action = getattr(args, "command", "")
        before = control_notifications.master(store, args.slug) if action in control_notifications.CONTROLS else None
        if action in clearance.COMMANDS:
            run_control(store, args, who, handler)
        else:
            handler(store, args)
        if action in control_notifications.CONTROLS:
            control_notifications.notify(store, args, LedgerClient(), before, action)
    except (SwarmError, InboxError) as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return 1
    return 0
