"""agentihooks swarm: run a swarm of Claude and Codex agents over a swarm ledger in herdr.

agentihooks swarm list | tick | templates
agentihooks swarm init NAME [--plan FILE] [--content FILE] [--repo DIR]  the one way to create a swarm and its ledger, paused
agentihooks swarm names check NAME [--plan FILE]                  valid or invalid against the swarm name rule, with suggestions
agentihooks swarm <id> create --repo DIR [--template NAME]         proof and demo swarms only; init calls it for every other swarm
agentihooks swarm <id> start | pause | stop [--now] | status
agentihooks swarm <id> names [--json]                              the swarm code, its herdr space and every agent name it gave
agentihooks swarm <id> url                                        print the ledger page link (create and start print it last)
agentihooks swarm <id> close [--note TEXT] [--now]                 a live master writes the note first; then summary, snapshot, all retired
agentihooks swarm <id> reopen                                     keep the summary and settings, start a fresh master
agentihooks swarm <id> take-master [--replace]                    this session becomes the master and prints its priming
agentihooks swarm <id> master up [--last | --new | --replace]     from a terminal: bring back the last master's conversation or start a new one
agentihooks swarm <id> master move --to HIVE                      order the live master to hand off so its successor starts on that hive
agentihooks swarm <id> set master-move-grace=SECONDS              how long a quiet master hive may stay quiet before the master moves (default 300)
agentihooks swarm <id> set hive-placement=on|off                  place this swarm's work on hives while a local machine stays local
agentihooks swarm <id> set local-max-agents=N                     with hive placement on, workers no hive takes run locally below N (default 8)
agentihooks swarm <id> <profile> up                               from a terminal: any role or overlay profile (planner, engineer, cicd, qa, frontend) in a pane for you, no task
agentihooks swarm <id> agent-up <profile>                         the same launch in its parser form
agentihooks swarm <id> remove                                     drop a swarm with no agents left, and its activity counts
agentihooks swarm <id> snapshot | restore [--from FILE]           save the swarm's state to its folder (stop does too); restore the newest, paused
agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N compact-limit=N   (or just: swarm <id> max-eng-agents=N)
agentihooks swarm <id> set snapshot-minutes=N                      automatic snapshot interval while running (default 30, 0 off)
agentihooks swarm <id> set eng-agent=claude|codex|auto eng-model=M eng-effort=E eng-kind=K eng-role=TEXT   (ci- likewise)
agentihooks swarm <id> set effort-min=E effort-max=E               every lane launch effort stays in this range (default medium, xhigh)
agentihooks swarm <id> set master-agent=claude|codex              master affinity; a change orders the live master to hand off to that harness
agentihooks swarm <id> save-template NAME                         write this swarm's lanes, caps and compact limit as a template
agentihooks swarm <id> send-message TEXT                          message to every live agent's inbox
agentihooks swarm <id> verdict FINDING VERDICT [--note TEXT]     master or operator judges a health finding
agentihooks swarm <id> classify EXECUTION lost|working --reason TEXT  master or operator classifies a suspect execution attempt
agentihooks swarm <id> terminal EXECUTION                         operator opens the worker terminal of an execution in herdr
agentihooks swarm <id> lift AGENT GATE                            operator or master lets one agent past a gate for one hour
agentihooks swarm <id> freeze | focus | unfreeze TARGET [--reason TEXT] [--quote WORDS]   operator, or master with his words
agentihooks swarm <id> cover STAGE ITEM --reason TEXT              operator, master or dispatcher marks a bottleneck stage owned by an open phases/ID or tasks/ID
agentihooks swarm <id> learned                                    list every seat's learned notes with seat and number
agentihooks swarm <id> promote SEAT NUMBER insight|canon --reason TEXT   raise a learned note; canon only by master or operator
agentihooks swarm <id> retire SEAT NUMBER --reason TEXT           master or operator retires a learned note from every later prompt
agentihooks swarm <id> culture set FILE | show                    the swarm's shared culture, read by every new occupant
agent side (name from --as or AGENTIHOOKS_AGENT_NAME):
agentihooks swarm <id> issue URL | pr URL | branch | done [--pr URL] | block NOTE | handoff DOC [--recap FILE] [--reason R] | say TEXT [--to NAME|eng|ci]
agentihooks swarm <id> learned TEXT [--maturity data|note|insight|canon]   (default note; canon only by the master)
agentihooks swarm <id> exit              an agent without a claim ends its own session
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
from urllib.parse import urlsplit

from hooks.context import injection_trace, quarantine
from scripts.doctor import priming
from scripts.gates import Who, catalog, intent, lift, modes, progress, quiet
from scripts.gates import log as gate_log
from scripts.gates.identity import refusal
from scripts.handoff import check as handoff_check
from scripts.handoff import envelope as handoff_envelope
from scripts.handoff import transfers
from scripts.handoff.resolve import Resolver
from scripts.inbox import exits, outbox, relay, wake
from scripts.inbox.seats import CANON, DEFAULT_MATURITY, MATURITIES, SeatError, is_seat, seat_address
from scripts.inbox.seats import PREFIX as SEAT_PREFIX
from scripts.inbox.store import InboxError, InboxStore
from scripts.routing.reservation import LaunchSlots
from scripts.swarm import (
    affinity,
    agent_up,
    bottleneck,
    bottleneck_status,
    clearance,
    control_notifications,
    default_models,
    delivery,
    dev_red,
    dispatch_seat,
    dispatcher,
    done_gate,
    failed_panes,
    idle,
    launch_check,
    ledger_client,
    ledger_events,
    ledger_probe,
    ledger_watchdog,
    master_launch,
    master_move,
    merge_queue,
    metrics,
    naming,
    outage,
    overlays,
    phase_planning,
    phase_state,
    phases,
    plan_review,
    priming_trace,
    prompt,
    queue_exit,
    queue_priority,
    queue_record,
    ready,
    reaper,
    reboot,
    release_wait,
    remote_control,
    review_rounds,
    snapshot,
    snapshot_job,
    stack,
    stages,
    stall_watch,
    take_master,
    templates,
    tick_heartbeat,
    tick_master,
    tick_stop,
    timer,
    timing,
    trace_plan,
    waits,
)
from scripts.swarm.health import activity
from scripts.swarm.health import findings as health
from scripts.swarm.ledger_client import LedgerClient, LedgerGone, LedgerRefused
from scripts.swarm.runtime import REPO_VARIABLE, HerdrRuntime
from scripts.swarm.status import (
    auto_snapshot,
    findings,
    live_agents,
    status_report,
    task_counts,
    transport,
    verdict_store,
)
from scripts.swarm.store import (
    APP_SERVER_TRANSPORT,
    ASSIST,
    AUTO_SCALING,
    AUTONOMY,
    DEFAULT_MASTERS_STEP,
    DELEGATE,
    DISPATCH,
    MASTER,
    RedisStore,
    SwarmConfig,
    SwarmError,
    Unreachable,
    connect,
)
from scripts.swarm.tick import agent_status, primed, ready_in_order, skip_refused, spawn_holds, tick
from scripts.swarm_ledger import ledger_creator, ledger_kinds, ledger_link, ledger_route, ledger_workspace, plan_shape
from scripts.swarm_v2 import master_scale, masters
from scripts.swarm_v2.runtime.routed import routed

SETTABLE = {
    "max-eng-agents": "max_eng",
    "max-ci-agents": "max_ci",
    "max-plan-agents": "max_plan",
    "masters-step": "masters_step",
    "compact-limit": "compact_limit",
    "snapshot-minutes": "snapshot_minutes",
    "master-move-grace": "master_move_grace",
    "local-max-agents": "local_max_agents",
}
LANE_KEYS = {f"{lane}-{key}": (lane, key) for lane in templates.LANES for key in templates.LANE_FIELDS}
EFFORT_KEYS = {"effort-min": "effort_min", "effort-max": "effort_max"}
SCALING_KEYS = {
    "scaling": "scaling",
    "load-high": "load_high",
    "load-low": "load_low",
    "memory-per-agent": "memory_per_agent_mb",
    "swap-full-share": "swap_full_share",
    "swap-floor": "swap_floor_mb",
}
ADDRESS_KEYS = {"api-url": "api_url", "api-url-pending": "pending_api_url"}
TOP_LEVEL = ("list", "tick", "templates", "rename", "waker", "prober", "waits", "tick-watch", "spoke")
NO_API_ADDRESS = "api-url takes an http or https address with a host and no credentials"
TRANSPORT_KEYS = {"codex-inbox-transport": "codex_inbox_transport"}
HIVE_PLACEMENT = "hive-placement"
ON_OFF = {"on": True, "off": False}
CONFIG_KEYS = {
    **SCALING_KEYS,
    **ADDRESS_KEYS,
    **default_models.KEYS,
    **TRANSPORT_KEYS,
    HIVE_PLACEMENT: "hive_placement",
    "engine": "engine",
}
GATE_KEYS = {f"{name}-gate": name for name in catalog.defaults()}
GATE_MODES = modes.MODES
RETIRES_MASTER = frozenset({"stop now", "close ledger"})
TICK_LOCK_MS = 10 * 60 * 1000
TICK_SECONDS = 60
# The scheduled tick holder stops a pass at scheduled_tick.TIMEOUT_S; leave an extra tick room to finish.
EXTRA_TICKS_UNTIL = 420
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ONLY_MASTER_CANON = "only the master or the operator makes a learned note canon"
ONLY_MASTER_RETIRE = "only the master or the operator retires a learned note"
PARKED_NEXT = "stop now; the task waits on its branch"
CONTROLLER_TICKS = "the controller tick spawns and reaps agents"
READ_ONLY_PARKED_NEXT = "stop now; the task waits on its dependencies and the next engineer continues it"
CLOSE_ASK = (
    "The operator pressed Close on the ledger page. Write one short paragraph in plain words on where the work "
    'stands, then run: agentihooks swarm {slug} close --note "<your paragraph>". Close retires you too.'
)


def now_ms():
    return int(time.time() * 1000)


def _publish_tick_status(store, slug, doc, view=None):
    from scripts.swarm import command_runner

    failure = sys.exception()
    try:
        timing.call(command_runner.publish, store, slug, doc, view)
    except Exception:
        if failure is None:
            raise


def _phase_steps(inbox, store, slug, doc, ledger):
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
    return actions


def _bin_stop(store, slug, ledger, runtime):
    store.redis.set(store.key(slug, "ledger-binned"), 1)
    _, left = stop_now(store, slug, runtime, ledger)
    return [
        f"the ledger is in the bin, still retiring {', '.join(left)}" if left else "the ledger is in the bin, stopped"
    ]


def _stopped_tick(store, slug, ledger, runtime, held):
    from scripts.swarm import command_runner, controller

    ledger = controller.FencedLedger(store, slug, held, ledger)
    runtime = controller.FencedRuntime(
        store, slug, held, runtime, controller.deployment_for(store.config(slug), os.environ)
    )
    controls = timing.call(command_runner.consume, store, slug)
    if timing.call(ledger.binned, slug):
        return controls + _bin_stop(store, slug, ledger, runtime), {}
    doc = timing.call(ledger.state, slug)
    store.redis.delete(*(store.key(slug, mark) for mark in tick_heartbeat.HALTS))
    return controls + timing.call(tick, slug, store, ledger, runtime, now_ms()), doc


@timing.instrument_tick
@outage.guard_tick
@ledger_route.service
def run_tick(store, slug, ledger=None, runtime=None, messenger=None, scheduled=False):
    from scripts.swarm import command_runner, commands, controller, incidents, lease, tick_activity

    if not ledger_link.local_mode(os.environ) and not scheduled:
        return [CONTROLLER_TICKS]
    tick_stop.check()
    ledger = ledger or LedgerClient()
    outbox.flush(InboxStore(store.redis))
    runtime = runtime or routed(herdr=HerdrRuntime())
    held = lease.acquire(store, slug, commands.hive_id())
    if held is None:
        return ["the swarm belongs to another hive"]
    token = controller.take_tick_lock(store, slug, held, TICK_LOCK_MS)
    if token is None:
        return ["another tick is running"]
    tick_stop.hold(token, lambda: controller.release_tick_lock(store, slug, token))

    def before_step():
        tick_stop.check()
        tick_activity.keep(store, slug, lambda: controller.keep_tick(store, slug, held, token, TICK_LOCK_MS), now_ms)

    keeping = timing.BEFORE_STEP.set(before_step)
    doc, view = {}, None
    try:
        timing.keep()
        if store.config(slug).state == "stopped":
            actions, doc = _stopped_tick(store, slug, ledger, runtime, held)
            return actions
        timing.call(relay.relay, store.redis)
        timing.call(LaunchSlots(store.redis).refresh_reported, now_ms())
        outage.providers(store, slug, runtime, now_ms())
        if isinstance(ledger, ledger_client.LedgerClient) and not outage.ledger_health(
            slug, now_ms(), lambda: isinstance(ledger.metadata(slug), dict)
        ):
            return ["Ledger health probe failed"]
        ledger = controller.FencedLedger(store, slug, held, ledger)
        runtime = controller.FencedRuntime(
            store,
            slug,
            held,
            runtime or routed(herdr=HerdrRuntime()),
            controller.deployment_for(store.config(slug), os.environ),
        )
        from scripts.swarm.health import prompt_backlog, spawn_stall, urgent_hold

        pressured = skip_refused(incidents.host_pressure, store, slug)
        restarted = pressured + timing.call(ledger_watchdog.watch, store, slug, ledger, runtime)
        probed = restarted + timing.call(ledger_probe.observe, store, slug, ledger, runtime, now_ms())
        timing.call(prompt_backlog.observe, store, slug, now_ms() // 1000)
        with spawn_stall.watch(store, slug, ledger, now_ms, runtime):
            controls = timing.call(command_runner.consume, store, slug)
            if timing.call(ledger.binned, slug):
                return _bin_stop(store, slug, ledger, runtime)
            inbox = InboxStore(store.redis)
            try:
                doc = timing.call(ledger.state, slug)
            except LedgerGone as exc:
                first = store.redis.set(store.key(slug, "ledger-gone"), 1, nx=True)
                return (
                    [f"{exc}; agentihooks swarm remove {slug} clears this swarm once it has no agents"] if first else []
                )
            store.redis.delete(*(store.key(slug, mark) for mark in tick_heartbeat.HALTS))
            timing.call(urgent_hold.observe, store, slug, doc, now_ms())
            actions = _phase_steps(inbox, store, slug, doc, ledger)
            doc, config = timing.call(ledger.state, slug), store.config(slug)
            view = timing.call(ledger_events.tick_view, inbox, store, slug, doc)
            mail, mode = ledger_events.Mail(inbox, store, slug), intent.mode_of(config)
            actions += skip_refused(
                intent.Check(
                    slug,
                    mode,
                    now_ms(),
                    ledger,
                    mail,
                    intent.pr_view,
                    intent.judged,
                    head=lambda url: getattr(view(url), "head", None),
                    named=intent.named_view,
                    text=intent.pr_text,
                ).run,
                doc,
            )
            actions += skip_refused(reboot.boot_pass, slug, store, ledger, runtime, now_ms())
            actions += timing.call(tick, slug, store, ledger, runtime or routed(herdr=HerdrRuntime()), now_ms())
            if store.config(slug).template == "doctor":
                from scripts.doctor import cli as doctor

                with lease.kept(store, slug, held):
                    actions += skip_refused(doctor.timer, store, slug, now_ms(), scheduled)
            timing.call(delivery.migrate_outbox, store, slug, inbox)
            agents = [a for a in timing.call(store.agents, slug) if a.state != "finished"]
            skip_refused(delivery.relay_to_page, inbox, slug, agents, ledger)
            doc, config = timing.call(ledger.state, slug), store.config(slug)
            view = timing.call(ledger_events.tick_view, inbox, store, slug, doc)
            actions += skip_refused(ledger_events.event_pass, inbox, store, slug, doc, ledger, now_ms(), view)
            actions += skip_refused(done_gate.recheck_pass, store, slug, doc, ledger, now_ms(), view)
            actions += skip_refused(progress.checks_pass, store.redis, slug, doc["tasks"], view, now_ms())
            rows = {t["id"]: t for t in doc["tasks"]}
            actions += skip_refused(waits.end_pass, store, slug, rows, inbox, view, now_ms(), ledger_events.view)
            actions += skip_refused(
                ready.merge_pass, ready.Pass(store, slug, ledger, view, ledger_events.view, now_ms()), rows
            )
            live = runtime.live_names() | {a.name for a in agents if a.runtime_backend != "local"}
            actions += skip_refused(
                stall_watch.stall_pass,
                stall_watch.Watch(
                    store,
                    slug,
                    ledger,
                    view,
                    frozenset(live),
                    lambda agent: runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)),
                    lambda lane: ready_in_order(slug, store, rows, doc, lane),
                    now_ms(),
                    released=release_wait.checker(config.repo),
                    reread=ledger_events.view,
                ),
                doc,
            )
            actions += skip_refused(quiet.quiet_pass, store, slug, rows, now_ms())
            actions += skip_refused(dispatcher.priorities, store, slug, doc, ledger, view, now_ms())
            if scheduled:
                from scripts.swarm import refresh_job

                timing.call(refresh_job.start, store, slug)
            found = timing.call(
                findings, store, slug, config, doc.get("tasks", []), doc.get("_meta", {}).get("events", []), view
            )
            actions += skip_refused(ledger_events.findings_pass, inbox, store, slug, found, doc)
            actions += timing.call(
                metrics.record_pass,
                slug,
                now_ms(),
                len(actions),
                os.environ,
                metrics.metrics_swarm.TickInput(store, doc, found, view, ledger),
            )
            actions += skip_refused(bottleneck_status.refresh, slug, store, now_ms())
            window = wake.window_ms(os.environ)
            actions += skip_refused(wake.maintenance, inbox, slug, agents, now_ms(), window)
            started = timing.call(snapshot_job.start, store, slug, now_ms(), os.environ)
            store.redis.set(store.key(slug, "last-tick"), now_ms())
            doc = timing.call(ledger.state, slug)
            tick_heartbeat.write(store, slug, now_ms(), doc)
            return probed + controls + actions + (["started automatic snapshot"] if started else [])
    finally:
        try:
            _publish_tick_status(store, slug, doc, view)
        finally:
            timing.BEFORE_STEP.reset(keeping)
            tick_stop.drop(token)
            controller.release_tick_lock(store, slug, token)


def cmd_list(store, args):
    for slug in store.slugs():
        c = store.config(slug)
        print(
            f"{naming.swarm_name(c.code) or '-'}\t{slug}\t{c.state}\teng {c.max_eng}\tci {c.max_ci}\tplan {c.max_plan}\tscaling {c.scaling}\tagents {len(store.agents(slug))}\t{c.repo}"
        )


@ledger_route.tick
def _tick_one(store, slug):
    try:
        if not _tickable(slug):
            return
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
        if started > until or tick_stop.STOP.is_set():
            return
        _tick_one(store, slug)


def _reconcile_services():
    try:
        installed = timer.reconcile(timer.entry_point())
    except (OSError, subprocess.SubprocessError) as exc:
        installed, why = False, f": {exc}"
    else:
        why = ""
    if not installed:
        print(f"warning: the swarm user services could not be installed or enabled{why}", file=sys.stderr)


@ledger_route.tick
def run_waits(store: RedisStore, slug: str, ledger=None) -> list[str]:
    from scripts.swarm import commands, lease

    held = lease.acquire(store, slug, commands.hive_id())
    if held is None:
        return []
    if not any(
        agent.state != "finished" and (idle.wait(store.redis, slug, agent.name) or {}).get("on")
        for agent in store.agents(slug)
    ):
        return []
    ledger = ledger or LedgerClient()
    doc = ledger.state(slug)
    inbox = InboxStore(store.redis)
    view = ledger_events.tick_view(inbox, store, slug, doc)
    lease.require(store, slug, held)
    return waits.end_pass(
        store, slug, {task["id"]: task for task in doc["tasks"]}, inbox, view, now_ms(), ledger_events.view
    )


@outage.guard_command
def cmd_waits(store, args):
    if why := timer.installed_refusal():
        raise SwarmError(f"the wait pass refused to run: {why}")
    try:
        if not timer.guard(timer.entry_point()):
            timing.emit(sys.stderr, "warning: the scheduled swarm tick guard could not enable the swarm tick timer")
    except (OSError, subprocess.SubprocessError) as exc:
        timing.emit(sys.stderr, f"the scheduled swarm tick guard failed: {type(exc).__name__}: {exc}")

    def check(slug):
        try:
            for action in run_waits(store, slug):
                timing.emit(sys.stdout, f"{slug}: {action}")
        except Exception as exc:
            timing.emit(sys.stderr, f"{slug}: {type(exc).__name__}: {exc}")

    slugs = store.slugs()
    if slugs:
        with ThreadPoolExecutor(max_workers=len(slugs)) as pool:
            list(pool.map(check, slugs))


def _tickable(slug):
    ledger = LedgerClient()
    try:
        return not (ledger.binned(slug) or ledger.closed(slug))
    except LedgerGone:
        return True


@outage.guard_command
def cmd_tick(store, args):
    from scripts.hive import workstation
    from scripts.swarm import scheduled_tick, tick_reaper

    if not workstation.runs_engine(os.environ):
        print(workstation.NO_ENGINE, file=sys.stderr)
        return
    started = time.monotonic()
    if why := timer.installed_refusal():
        raise SwarmError(f"the tick refused to run: {why}")
    try:
        with scheduled_tick.tick_lock():
            for left in tick_reaper.reap():
                print(
                    f"reaped leftover swarm tick process {left.pid} after {left.age_s} seconds "
                    f"with {left.signal}: {left.command}"
                )
            _tick_host(store, started)
    except scheduled_tick.AlreadyRunning:
        print("another swarm tick is running on this machine", file=sys.stderr)


def _tick_host(store, started):
    from scripts import operator_env
    from scripts.swarm_ledger import authority_context

    operator_env.fill(os.environ)
    if os.environ.get("AGENTIHOOKS_CONTROLLER_TICK_SECONDS"):
        raise SwarmError(
            "the tick refused to run: AGENTIHOOKS_CONTROLLER_TICK_SECONDS is for controller installs, "
            "and the host scheduler ticks every 60 seconds"
        )
    operator_env.classifier(os.environ)
    with authority_context.for_tick(store) as slugs:
        _reconcile_services()
        if slugs:
            firsts, until = _Firsts(len(slugs)), time.monotonic() + EXTRA_TICKS_UNTIL
            if tick_stop.run(
                slugs,
                lambda slug: _tick_while_others_run(store, slug, firsts, until),
                firsts.settled.set,
                time.monotonic() - started,
            ):
                return
        from scripts import herdr_gc

        try:
            for line in herdr_gc.run(dict(os.environ), now_ms(), True, swarm_only=True):
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


def cmd_prober(store, args):
    from scripts.swarm import command_runner, quota_refresh

    quota_refresh.run(store, lambda slug, row: command_runner.execute(store, slug, row), quota_refresh.WAIT_S)


def cmd_spoke(store, args):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import workstation
    from scripts.swarm import commands
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    if workstation.runs_engine(os.environ):
        raise SwarmError(workstation.ENGINE_RUNS)
    bus = factory.open_bus(replace(BusConfig.from_environ(os.environ), provision=False))
    try:
        workstation.run(store, bus, LocalHerdrRuntime(HerdrRuntime()), commands.hive_id())
    finally:
        bus.close()


def cmd_tick_watch(store, args):
    from scripts.swarm import scheduled_tick, tick_watch
    from scripts.swarm.tick import place_master, respawn_master
    from scripts.swarm_ledger import ledger_link

    runtime = routed(herdr=HerdrRuntime())
    if ledger_link.local_mode(os.environ):

        def respawn(slug):
            return respawn_master(slug, store, runtime, now_ms())

        repair = tick_watch.Repair(scheduled_tick.restart, respawn)
    else:

        def repair(slug, now):
            return place_master(slug, store, runtime, now)

    tick_watch.run(store, now_ms, repair)


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
        scratch=not ledger_creator.shared(os.environ),
        caps_pinned=args.max_eng_agents is not None or args.max_ci_agents is not None,
        codex_inbox_transport=APP_SERVER_TRANSPORT,
        masters_step=DEFAULT_MASTERS_STEP,
    )
    store.create(config)
    from scripts.swarm import commands

    commands.bind(store, args.slug, commands.hive_id())
    print(
        json.dumps(
            {
                "created": args.slug,
                "repo": repo,
                "state": "paused",
                "template": args.template,
                "scaling": config.scaling,
            }
        )
    )
    print(ledger_link.page_line(args.slug))


def _state(store, args, state):
    if state == "running":
        store.redis.delete(store.key(args.slug, "master-retired-tasks"))
    store.update(args.slug, state=state)
    if state == "running" and not timer.ensure(timer.entry_point()):
        print(
            "warning: the scheduled swarm tick or swarm services could not be enabled; "
            "run agentihooks swarm tick yourself",
            file=sys.stderr,
        )
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


def cmd_proof_control(store, args):
    from scripts.swarm_ledger import proof_ledger

    result = proof_ledger.control(args.slug, json.loads(Path(args.request).read_bytes()))
    print(json.dumps(result))


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
        row = rows.get(agent.task, {})
        reopen = agent.state != "finished" and row.get("state") in ("claimed", "pr")
        reopen = reopen and row.get("claimed_by") == agent.name
        if reopen:
            store.refund_held_claim(slug, agent.task, agent.name, store.claim_generation(slug, agent.task))
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)
        if reopen:
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
    by = _caller(args) or "operator"
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
    by = _caller(args) or "operator"
    LedgerClient().reopen(args.slug, by)
    _state(store, args, "running")


def cmd_take_master(store, args):
    runtime = routed(herdr=HerdrRuntime())
    if args.slug not in store.slugs():
        snapshot.recreate(store, args.slug, runtime.live_names())
    from scripts.gates import Who

    name = Who.from_env().name
    ledger = LedgerClient()

    def admit(master):
        if ledger.closed(args.slug):
            ledger.reopen(args.slug, master)

    record, transfer = take_master.take(store, args.slug, name, runtime, now_ms(), args.replace, admit)
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
    if args.action == "move":
        print(json.dumps(master_move.order(store, args.slug, args.to, now_ms())))
        return
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
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent and agent.lane != MASTER and store.claimant(args.slug, agent.task) != name:
        _retire(store, args.slug, agent, "finished its session without a claim")
    else:
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
    return getattr(config, {**SETTABLE, **EFFORT_KEYS, **CONFIG_KEYS}.get(key, key))


def lifecycle_control(args):
    if args.command == "close":
        return "close ledger"
    return "stop now" if vars(args).get("now") else args.command


def control_readings(store, args):
    config = store.config(args.slug)
    if args.command == "set":
        keys = (pair.partition("=")[0] for pair in args.pairs)
        return {
            key: setting(config, key)
            for key in keys
            if key in (*SETTABLE, *LANE_KEYS, *EFFORT_KEYS, *CONFIG_KEYS, *GATE_KEYS, "autonomy")
        }
    if args.command == "lift":
        agent = _lifted_agent(store, args)
        lifted = lift.held(args.slug, agent.name, agent.task, args.gate)
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
    if key in ("memory-per-agent", "swap-floor"):
        if not value.isdigit():
            raise SwarmError(f"{key} takes a whole number of MB")
        return int(value)
    try:
        return float(value)
    except ValueError:
        what = "the share of swap in use" if key == "swap-full-share" else "the one minute load per CPU"
        raise SwarmError(f"{key} takes a number, {what}") from None


def api_address(value):
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise SwarmError(NO_API_ADDRESS) from None
    if value and (parts.scheme not in ("http", "https") or not host or "@" in parts.netloc):
        raise SwarmError(NO_API_ADDRESS)
    return value


def config_value(key, value):
    if key in default_models.KEYS or key in TRANSPORT_KEYS or key == "engine":
        return value
    if key == HIVE_PLACEMENT:
        return on_off(value)
    return api_address(value) if key in ADDRESS_KEYS else scaling_value(key, value)


def on_off(value):
    if value not in ON_OFF:
        raise SwarmError("hive-placement takes on or off")
    return ON_OFF[value]


def _split_master_pairs(pairs, key, parse):
    """The master count and its scaling rule sit beside the swarm config: checked with the other pairs, stored after."""
    split = [(pair, *pair.partition("=")) for pair in pairs]
    values = [parse(value) for _, name, _, value in split if name == key]
    return values, [pair for pair, name, _, _ in split if name != key]


def _store_master_counts(store, slug, counts, owners):
    seats = masters.MasterSeats(store.redis)
    for count in counts:
        seats.set_count(slug, count)
    for hive, owner in owners:
        seats.own_hive(slug, hive, owner)


def _store_master_rules(store, slug, rules):
    for rule in rules:
        master_scale.set_rule(store.redis, slug, rule)


def _pinned(changes):
    return {"caps_pinned": True} if {"max_eng", "max_ci"} & changes.keys() else {}


def _manual_count_step(counts):
    return {"masters_step": 0} if counts else {}


def cmd_set(store, args):
    from scripts.swarm import ledger_cutover

    changes, lanes = {}, {key: dict(value) for key, value in store.config(args.slug).lanes.items()}
    counts, pairs = _split_master_pairs(args.pairs, "masters", masters.count_of)
    changes.update(_manual_count_step(counts))
    rules, pairs = _split_master_pairs(pairs, "master-per", master_scale.rule_of)
    hives, pairs = masters.hive_pairs(args.slug, pairs)
    masters.MasterSeats(store.redis).check_hives(args.slug, counts, hives)
    for pair in pairs:
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
        if key in CONFIG_KEYS:
            changes[CONFIG_KEYS[key]] = config_value(key, value)
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
                f"set takes {', '.join(SETTABLE)}=<whole number>, masters=N, master-per=N|hive|off, "
                f"{masters.HIVE_KEY}<hive id>=<master seat>, autonomy={'|'.join(AUTONOMY)}, "
                f"effort-min=E, effort-max=E, claude-model=M, codex-model=M, scaling=auto|manual, load-high=N, load-low=N, memory-per-agent=MB, swap-full-share=N, swap-floor=MB, api-url=URL, codex-inbox-transport=legacy|app_server "
                f"or {', '.join(LANE_KEYS)}=<value>, engine=legacy|events"
            )
        changes[SETTABLE[key]] = int(value)
    config = ledger_cutover.settings(store, args.slug, {**changes, **_pinned(changes)})
    _store_master_counts(store, args.slug, counts, hives)
    _store_master_rules(store, args.slug, rules)
    asked = any(pair.startswith("master-agent=") for pair in args.pairs)
    master = affinity.order(store, args.slug, now_ms()) if asked else affinity.pending(store, args.slug)
    print(
        json.dumps(
            {
                "swarm": args.slug,
                "applies": "next tick",
                "max_eng": config.max_eng,
                "max_ci": config.max_ci,
                "max_plan": config.max_plan,
                "masters_step": config.masters_step,
                "compact_limit": config.compact_limit,
                "autonomy": config.autonomy,
                "snapshot_minutes": config.snapshot_minutes,
                "effort_min": config.effort_min,
                "effort_max": config.effort_max,
                "claude_model": config.claude_model,
                "codex_model": config.codex_model,
                "lanes": config.lanes,
                "overlays": config.overlays,
                "scaling": config.scaling,
                "load_high": config.load_high,
                "load_low": config.load_low,
                "memory_per_agent_mb": config.memory_per_agent_mb,
                "swap_full_share": config.swap_full_share,
                "swap_floor_mb": config.swap_floor_mb,
                "hive_placement": config.hive_placement,
                "local_max_agents": config.local_max_agents,
                "api_url": config.api_url,
                "codex_inbox_transport": config.codex_inbox_transport,
                "engine": config.engine,
                "master_affinity": {"desired": affinity.desired(config) or "auto", "order": master},
                "masters": masters.MasterSeats(store.redis).count(args.slug),
                "master_per": master_scale.rule(store.redis, args.slug) or master_scale.OFF,
                "hive_owners": masters.MasterSeats(store.redis).hive_owners(args.slug),
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


def cmd_shutdown(store, args):
    doc = reboot.prepare(store, args.slug, now_ms())
    names = [a["name"] for a in doc["agents"]]
    print(json.dumps({"swarm": args.slug, "record": str(reboot.record_path(args.slug)), "local_agents": names}))


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


def _remote_control_text(agents):
    return "".join(f"{line}\n" for a in agents if (line := remote_control.line(a.__dict__)))


def cmd_ledger_ready(store: RedisStore, args: argparse.Namespace) -> None:
    from scripts.swarm import ledger_cutover

    print(json.dumps(ledger_cutover.readiness(store, args.slug)))


def cmd_status(store, args):
    if args.json:
        store.config(args.slug)
        print(json.dumps(status_report(store, args.slug, LedgerClient().state(args.slug), HerdrRuntime())))
        return
    outage.show(args.slug, False)
    config = store.config(args.slug)
    agents = live_agents(store.agents(args.slug), HerdrRuntime())
    ledger = LedgerClient()
    doc = ledger.state(args.slug)
    tasks = doc["tasks"]
    counts = task_counts(tasks)
    found = findings(store, args.slug, config, tasks, ledger.events(args.slug))
    print(
        f"{naming.swarm_name(config.code) or '-'}  {config.slug}  {config.state}  eng {config.max_eng}  ci {config.max_ci}  plan {config.max_plan}  masters step {config.masters_step}  scaling {config.scaling}  effort {config.effort_min} to {config.effort_max}  repo {config.repo}"
    )
    print(
        "gate modes  "
        + "  ".join(f"{name}={modes.label(mode)}" for name, mode in catalog.current(config.gates).items())
    )
    print("tasks  " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    print(plan_shape.report(tasks, config.max_eng)["summary"])
    for phase_id, state, held in phase_state.report(doc):
        print(f"phase {phase_id}  {state}" + (f"  holds {', '.join(held)}" if held else ""))
    from scripts.swarm import capacity, incidents, quota_view
    from scripts.swarm_ledger import ledger_freezes

    quota = quota_view.lines(capacity.read(store, args.slug), now_ms())
    held = spawn_holds(store, args.slug) + failed_panes.status_lines(store, args.slug)
    held += incidents.status_lines(store, args.slug)
    from scripts.hive import admission, placement_waits

    held += admission.status_lines(placement_waits.read(store, args.slug))
    for line in ledger_freezes.lines(doc) + quota + held + outbox.status_lines():
        print(line)
    print(bottleneck.line(bottleneck.read(store, args.slug), now_ms()))
    print(stages.line(stages.read(store, args.slug), now_ms()))
    print(bottleneck_status.line(bottleneck_status.report(store, args.slug, now_ms())))
    print(_snapshot_line(auto_snapshot(config)))
    print(_affinity_line(affinity.report(store, args.slug, config, agents)))
    sys.stdout.write(_remote_control_text(agents))
    if promotion := tick_master.status_line(tick_master.read(store, args.slug)):
        print(promotion)
    print(
        "Agent\tLane\tHarness\tProfile\tModel\tAccount\tPane\tTask\tState\tConversation\tPush inbox\tNative terminal\tModel source\tConfidence"
    )
    for a in agents:
        model = " ".join(filter(None, (a.model, a.effort))) if a.model else "unknown"
        can = transport(a)
        push = "yes" if can["push_inbox"] else "no"
        attach = "yes" if can["native_terminal_attach"] else "no"
        print(
            f"{a.name}\t{a.lane}\t{a.harness}\t{a.profile or 'unknown'}\t{model}\t{a.account or '-'}\t{a.pane_id}\t{a.task}\t{agent_status(a)}\t{a.conversation_id or '-'}\t{push}\t{attach}\t{a.model_source or '-'}\t{a.model_confidence if a.model_confidence is not None else '-'}"
        )
    from scripts.hive import join_report

    sys.stdout.write(join_report.text(store.redis, agents))
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


def cmd_cover(store, args):
    from scripts.swarm import stage_cover

    by = clearance.coverer(store, args.slug, Who.from_env())
    doc = LedgerClient().state(args.slug)
    request = stage_cover.Request(args.stage, args.item, args.reason, by)
    print(json.dumps(stage_cover.cover(store, args.slug, doc, request, now_ms())))


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


def cmd_merge_path(store, args):
    from scripts.swarm import merge_path

    if args.hours <= 0:
        raise SwarmError("merge-path takes hours above zero")
    until_s = now_ms() / 1000
    repo, found = merge_path.collect(store.config(args.slug).repo, (until_s - args.hours * 3600, until_s))
    line = merge_path.render(repo, found)
    print(json.dumps({"repo": repo, **found}) if args.json else line)
    if args.post:
        LedgerClient().comment_item(args.slug, args.post, line)


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


def _caller(args):
    return args.name or Who.from_env().name


def _master_or_operator(store, args, message):
    store.config(args.slug)
    name = _caller(args)
    if name in ("", "operator"):
        session = Who.from_env()
        if clearance.is_operator(store, session):
            return "operator"
        name = session.name
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    if agent is None or agent.lane != MASTER:
        raise SwarmError(message)
    return name


def cmd_verdict(store, args):
    by = args.by = _master_or_operator(store, args, "only the master or the operator gives a finding a verdict")
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
    from scripts.gates import entry

    store.config(args.slug)
    agent = _lifted_agent(store, args)
    if args.gate not in {*entry.GATES, *lift.SERVER_GATES}:
        raise SwarmError(f"no gate named {args.gate}")
    pull = {} if args.gate in lift.SERVER_GATES else _lifted_pull(args.slug, agent.task)
    lift.lift_agent(Who(name=agent.name, swarm=args.slug, task=agent.task), args.gate, pull=pull)
    bound = {"head": pull["head"]} if pull else {"minutes": lift.LIFT_SECONDS // 60}
    print(json.dumps({"agent": agent.name, "gate": args.gate, **bound}))


def _lifted_agent(store, args):
    agent = next((a for a in store.agents(args.slug) if a.name == args.agent), None)
    if agent is None:
        raise SwarmError(f"{args.agent} is not in this swarm")
    return agent


def _lifted_pull(slug, task):
    url = task and (LedgerClient().task(slug, task) or {}).get("pr_url")
    if not url:
        return {}
    head = intent.pr_head(url)
    if not head:
        raise SwarmError(f"could not read the head of {url}; lift again once GitHub answers")
    return {"url": url, "head": head}


def cmd_send_message(store, args):
    store.config(args.slug)
    sender = _caller(args) or delivery.OPERATOR
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


def _local_repo(store, slug):
    local = os.environ.get(REPO_VARIABLE)
    return local if local and os.environ.get("AGENTIHOOKS_SWARM") == slug else store.config(slug).repo


def cmd_issue(store, args):
    agent = _worker(store, args)
    LedgerClient().update_task(args.slug, agent.task, {"issue_url": args.url}, by=agent.name)
    print(json.dumps({"task": agent.task, "issue_url": args.url}))


def cmd_review(store, args):
    agent = _worker(store, args)
    row = LedgerClient().task(args.slug, agent.task) or {}
    pulls = row.get("split_pulls") or []
    print(
        json.dumps(
            review_rounds.start(args.slug, agent.task, pr_url=row.get("pr_url"), first_pr=pulls[0] if pulls else "")
        )
    )


def _git(run, *args):
    try:
        return run(["git", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SwarmError(f"git {args[0]} could not run: {exc}") from exc


def worktree_branch(recorded, run=subprocess.run):
    current = _git(run, "branch", "--show-current")
    branch = current.stdout.strip() if current.returncode == 0 else ""
    if not branch:
        raise SwarmError("swarm branch runs in a worktree on a branch; this checkout is on none")
    target = branch
    if recorded and recorded != branch:
        target = f"HEAD:{recorded}"
        if _tracks(run, recorded):
            branch = recorded
    if _git(run, "ls-remote", "--exit-code", "--heads", "origin", branch).returncode != 0:
        raise SwarmError(f"branch {branch} is not on origin; push it first with git push -u origin {target}")
    return branch


def _tracks(run, recorded):
    done = _git(run, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    return done.returncode == 0 and done.stdout.strip() == f"origin/{recorded}"


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


def cmd_prepare(store, args):
    from scripts.swarm import publish

    fields = publish.prepare(args.slug, _worker(store, args), _local_repo(store, args.slug), LedgerClient())
    print(json.dumps(fields))


def cmd_publish(store, args):
    from scripts.swarm import publish

    fields = publish.publish(
        args.slug, _worker(store, args), _local_repo(store, args.slug), LedgerClient(), args.message, args.files
    )
    print(json.dumps(fields))


def cmd_branch(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    row = ledger.task(args.slug, agent.task) or {}
    branch = worktree_branch(row.get("branch"))
    fields = {"branch": branch, "branch_repo": origin_repo()}
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    carried = {"pr_url": row["pr_url"]} if row.get("pr_url") else {}
    print(json.dumps({"task": agent.task, **fields, **carried}))


def cmd_pr(store, args):
    agent = _worker(store, args)
    config = store.config(args.slug)
    awaiting = "approval" if config.autonomy == ASSIST else ""
    head, repo = pull_branch(args.url)
    branch = {"branch": head, "branch_repo": repo} if head else {}
    fields = {"pr_url": args.url, "state": "pr", "awaiting": awaiting, **branch}
    ledger = LedgerClient()
    doc = ledger.state(args.slug)
    row = next(t for t in doc["tasks"] if t.get("id") == agent.task)
    fields.update(_split_binding(row, args.url, args.half))
    scoped = _pull_scope(row, args)
    row.update(scoped)
    fields.update(scoped)
    checked = intent.stamp(args.slug, agent.task, args.url, doc, intent.mode_of(config), now_ms())
    if row.get("pr_url") != args.url:
        lift.drop_task(args.slug, agent.task)
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    print(json.dumps({"task": agent.task, "pr_url": args.url, **branch, "intent": checked}))


def _split_binding(row, url, half):
    pulls, halves = row.get("split_pulls") or [], row.get("split") or []
    if not halves:
        if half:
            raise SwarmError("this task is not split: drop --half")
        return {}
    if url in pulls:
        if half and half != pulls.index(url) + 1:
            raise SwarmError(f"{url} is half {pulls.index(url) + 1} of this split task")
        return {}
    if len(pulls) >= len(halves):
        raise SwarmError(f"every half of this split task has a pull request: {', '.join(pulls)}")
    if not 1 <= half <= len(halves):
        raise SwarmError(
            f"this task is split in {len(halves)} halves: name this pull request's half with --half {len(pulls) + 1}"
        )
    if half != len(pulls) + 1:
        raise SwarmError(f"half {len(pulls) + 1} of this split task is next: record it before half {half}")
    return {"split_pulls": [*pulls, url]}


PULL_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


def _pull_scope(row, args):
    scopes = row.get("pull_scopes") or {}
    kind = "stage" if args.stage is not None else "prerequisite" if args.prerequisite is not None else ""
    if not kind:
        return {"pull_scopes": {url: s for url, s in scopes.items() if url != args.url}} if args.url in scopes else {}
    scope = getattr(args, kind).strip()
    if not scope:
        raise SwarmError(f"--{kind} needs the scope this pull request delivers")
    if not PULL_URL.fullmatch(args.url):
        raise SwarmError(f"--{kind} needs a GitHub pull request URL, not {args.url}")
    return {"pull_scopes": {**scopes, args.url: {"kind": kind, "scope": scope}}}


def _queue_intent(store, args, agent, head):
    state = intent.Verdicts(args.slug, intent.NAME)
    who = Who(name=agent.name, swarm=args.slug, task=agent.task)
    decision = intent.queue_decision(who, state, args.url, intent.mode_of(store.config(args.slug)), head)
    if not decision.allowed:
        gate_log.append(args.slug, gate_log.Row.of(intent.NAME, "deny", who, "Bash", decision.reason))
        raise SwarmError(decision.reason)


def cmd_merge(store, args):
    if args.action == "priority":
        sender = _master_or_operator(store, args, "only the master or the operator prioritizes the merge queue")
        tasks = LedgerClient().tasks(args.slug)
        if args.lift and not clearance.is_operator(store, Who.from_env()):
            raise SwarmError("only the operator lifts a running merge group priority hold")
        result = queue_priority.request(store, args.slug, args.url, sender, tasks, lift=args.lift)
    else:
        agent = _worker(store, args) if args.action != "state" else None
        if agent is not None:
            done_gate.require_local(store, args.slug, agent.task)
            done_gate.require_target(store, args.slug, agent.task, args.url, LedgerClient().tasks(args.slug))
        if args.action == "queue":
            result = merge_queue.operate(
                args.action,
                args.url,
                admit=lambda head: _queue_intent(store, args, agent, head) or queue_exit.require_fresh(args.url, head),
                journal=queue_record.Journal(args.url),
            )
        else:
            result = merge_queue.operate(args.action, args.url, journal=queue_record.Journal(args.url))
    print(json.dumps(result))


def cmd_done(store, args):
    agent = _worker(store, args)
    if agent.lane == dispatch_seat.LANE:
        return _dispatcher_done(store, args.slug, agent)
    done_gate.require_local(store, args.slug, agent.task)
    ledger = LedgerClient()
    row = next((t for t in ledger.tasks(args.slug) if t.get("id") == agent.task), {})
    proof = {key: getattr(args, f"proof_{key}") for key in ledger_kinds.PROOF_KEYS if getattr(args, f"proof_{key}")}
    proven = {**row, "proof": {**(row.get("proof") or {}), **proof}}
    missing = ledger_kinds.unmet(proven)
    if missing:
        if ledger_kinds.kind(row) == "research":
            raise SwarmError(
                "a research task is done only with its proof: "
                "--finding must be a single link to the artifact; put prose in a task comment"
            )
        flags = ", ".join("--" + key.replace("_", "-").replace(" or ", " or --") for key in missing)
        raise SwarmError(f"a {ledger_kinds.kind(row)} task is done only with its proof: give {flags}")
    url = args.pr or row.get("pr_url")
    pull = ledger_events.view(url) if done_gate.gated(proven) and url else None
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
    refused = done_gate.refusal(proven, url, _pull_reader(url, pull))
    if refused:
        raise SwarmError(refused)
    fields = {"state": "done", **({"pr_url": args.pr} if args.pr else {}), **({"proof": proof} if proof else {})}
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    _close_members(ledger, args.slug, agent, row, {**fields, "pr_url": args.pr or row.get("pr_url")})
    waits.settle_notices(InboxStore(store.redis), agent, "done")
    _retire(store, args.slug, agent, "finished its task and exited")
    fallback = {"pull_request": pull.fallback} if pull is not None and pull.fallback else {}
    print(
        json.dumps({"task": agent.task, "state": "done", **fallback, "next": "stop now; the swarm closes this session"})
    )


def _pull_reader(url, pull):
    return lambda target: pull if target == url else ledger_events.view(target)


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
    from scripts.swarm import publish

    pushed = publish.pushed_branches(_local_repo(store, args.slug), rows[agent.task], agent)
    red_run = _dev_red_run(store, args.slug) if args.dev_red else None
    if pushed:
        ledger.update_task(args.slug, agent.task, pushed, by=agent.name)
    block_agent(store, args.slug, agent, args.note, ledger, red_run)
    cause = {"dev_red_run": red_run} if red_run is not None else {}
    print(
        json.dumps({"task": agent.task, "state": "blocked", **cause, "next": "stop now; the swarm closes this session"})
    )


def _dev_red_run(store, slug):
    try:
        red_run = dev_red.failing_run(_local_repo(store, slug))
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
    if red_run is not None:
        store.refund_held_claim(slug, agent.task, agent.name, store.claim_generation(slug, agent.task))
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
    held = waits.on(*args.on, task=agent.task) if args.on else None
    if held is None and (args.minutes or 0) <= 0:
        raise SwarmError("a wait lasts a whole number of minutes above zero")
    if held is None and args.minutes > waits.BARE_MAX_MINUTES:
        raise SwarmError(
            f"a bare wait lasts at most {waits.BARE_MAX_MINUTES} minutes; wait on checks, a reply or a task with --on"
        )
    if held is not None:
        rows = waits.named_tasks(
            held["kind"], held["target"], agent.task, lambda task_id: LedgerClient().task(args.slug, task_id)
        )
        get = InboxStore(store.redis).get
        if problem := waits.target_problem(held["kind"], held["target"], agent.task, rows, get):
            raise SwarmError(problem)
        if held["kind"] == "checks" and waits.PULL_URL.fullmatch(held["target"]):
            head = ledger_events.head(held["target"])
            if head == "":
                raise SwarmError("cannot read the pull request head; retry the checks wait")
            if head is None:
                waits.mutation_wait.admitted_pending(held["target"])
            held["head"] = head
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
                **({"on": held, "next": waits.FINISH_TURN} if held else {}),
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
    store.mark_restart(args.slug, agent.task, "park")
    _hand_off(store, args.slug, agent, text, "exit", ledger)
    removed = stack.remove_worktree(top) if top else ""
    after = PARKED_NEXT if top else READ_ONLY_PARKED_NEXT
    print(json.dumps({"task": agent.task, **fields, "worktree_removed": removed, "next": after}))


def cmd_ready(store, args):
    agent = _worker(store, args)
    if agent.state == "finished":
        raise SwarmError(f"{agent.name} already handed off its seat; the swarm carries its pull request")
    done_gate.require_local(store, args.slug, agent.task)
    text = _read(args.doc, "handoff document")
    ledger = LedgerClient()
    row = next((t for t in ledger.tasks(args.slug) if t.get("id") == agent.task), {})
    found = ready.check(
        row,
        agent,
        store.config(args.slug).autonomy,
        ledger_events.view,
        now_ms(),
        ready.continuation(store, args.slug, agent.task),
    )
    problems = handoff_check.problems(text, Resolver(args.slug, store.redis, ledger.state))
    if problems:
        raise SwarmError(handoff_check.refusal(problems))
    ready.release(store, args.slug, agent.task, found, ledger, agent.name, ledger_events.view)
    try:
        _hand_off(store, args.slug, agent, text, "exit", ledger)
    except Exception:
        ready.withdraw(store, args.slug, agent.task, ledger, agent.name)
        raise
    print(
        json.dumps(
            {
                "task": agent.task,
                "pr_url": found["url"],
                "head": found["head"],
                "awaiting": ready.AWAITING,
                "next": "stop now; the swarm carries the pull request to merge and closes this session",
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
    from scripts.swarm_v2 import restore_decisions

    target = next((a for a in store.agents(args.slug) if a.name == store.names.resolve(args.agent)), None)
    if target is not None and target.runtime_backend == "kubernetes":
        print(json.dumps(restore_decisions.decide(store, args.slug, target, args.choice, Who.from_env())))
        return
    name = _caller(args)
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
    name = _caller(args)
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
    name = _caller(args) or "operator"
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


def cmd_masters_channel(store, args):
    from scripts.swarm_v2.masters_channel import MastersChannel

    channel, name = MastersChannel(store.redis), store.names.resolve(Who.from_env().name)
    if args.action == "say":
        print(json.dumps(asdict(channel.post(args.slug, name, args.text, time.time()))))
        return
    posts = channel.read(args.slug, name) if args.all else channel.unread(args.slug, name)
    print(json.dumps([asdict(post) for post in posts]))


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
    exits.settle(InboxStore(store.redis), agent.name, agent.seat if agent.lane == DISPATCH else "", exit_text)


def cmd_say(store, args):
    agent = _me(store, args)
    if args.to in ("", delivery.OPERATOR):
        LedgerClient().say(args.slug, args.text, by=agent.name)
        print(json.dumps({"posted": True}))
        return
    sent = delivery.send(store, args.slug, args.text, sender=agent.name, to=args.to, fyi=args.fyi)
    print(json.dumps({"sent": sent}))


def queued_say(args, who) -> bool:
    """A swarm say to agents that met a Redis outage is queued in the outbox, its recipients looked up on replay."""
    if vars(args).get("command") != "say" or args.to in ("", delivery.OPERATOR):
        return False
    print(json.dumps(delivery.queue(args.slug, args.text, args.name or who.name, args.to, fyi=args.fyi)))
    return True


def cmd_terminal(store, args):
    from scripts.codex_session import attach

    try:
        opened = attach.local_terminal(store, args.slug, args.execution, dict(os.environ))
    except attach.AttachError as exc:
        raise SwarmError(str(exc)) from None
    if opened is not None:
        print(json.dumps(opened))
        return
    from scripts.swarm_v2.kubernetes import operator_terminal

    print(json.dumps(operator_terminal.command(store, args.slug, args.execution, os.environ, Who.from_env())))


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
    for plain in ("start", "pause", "remove", "snapshot", "shutdown", "url", "reopen", "rename"):
        sub.add_parser(plain)
    sub.add_parser("restore").add_argument("--from", dest="source", default="")
    sub.add_parser("stop").add_argument("--now", action="store_true")
    close = sub.add_parser("close")
    close.add_argument("--note", default="")
    close.add_argument("--now", action="store_true")
    sub.add_parser("take-master").add_argument("--replace", action="store_true")
    sub.add_parser("proof-control").add_argument("--request", required=True)
    master_actions = sub.add_parser("master").add_subparsers(dest="action", required=True)
    master_up = master_actions.add_parser("up")
    master_actions.add_parser("move").add_argument("--to", required=True)
    pick = master_up.add_mutually_exclusive_group()
    pick.add_argument("--last", dest="choice", action="store_const", const=master_launch.LAST, default="")
    pick.add_argument("--new", dest="choice", action="store_const", const=master_launch.NEW)
    pick.add_argument("--replace", dest="choice", action="store_const", const=master_launch.REPLACE)
    sub.add_parser("agent-up").add_argument("profile")
    sub.add_parser("exit")
    sub.add_parser("set").add_argument("pairs", nargs="+")
    sub.add_parser("save-template").add_argument("template_name", metavar="name")
    sub.add_parser("status").add_argument("--json", action="store_true")
    sub.add_parser("ledger-ready")
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
    path = sub.add_parser("merge-path")
    path.add_argument("--hours", type=float, default=24.0)
    path.add_argument("--json", action="store_true")
    path.add_argument("--post")
    verdict = sub.add_parser("verdict")
    verdict.add_argument("finding")
    verdict.add_argument("verdict")
    verdict.add_argument("--note", default="")
    sub.add_parser("terminal").add_argument("execution")
    classify = sub.add_parser("classify")
    classify.add_argument("execution_id")
    classify.add_argument("ruling", choices=("lost", "working"))
    classify.add_argument("--reason", required=True)
    sub.add_parser("send-message").add_argument("text")
    lift = sub.add_parser("lift")
    lift.add_argument("agent")
    lift.add_argument("gate")
    cover = sub.add_parser("cover")
    cover.add_argument("stage")
    cover.add_argument("item")
    cover.add_argument("--reason", required=True)
    sub.add_parser("issue").add_argument("url")
    pr = sub.add_parser("pr")
    pr.add_argument("url")
    pr.add_argument("--half", type=int, default=0)
    declared = pr.add_mutually_exclusive_group()
    declared.add_argument("--stage")
    declared.add_argument("--prerequisite")
    merge = sub.add_parser("merge")
    merge.add_argument("action", choices=("queue", "dequeue", "state", "priority"))
    merge.add_argument("url")
    merge.add_argument(
        "--lift", action="store_true", help="operator: prioritize immediately despite a running head group"
    )
    sub.add_parser("prepare")
    publish = sub.add_parser("publish")
    publish.add_argument("--message", "-m", required=True)
    publish.add_argument("files", nargs="*")
    sub.add_parser("branch")
    sub.add_parser("review")
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
    sub.add_parser("ready").add_argument("doc")
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
    decision.add_argument("choice", choices=("resume", "native", "context", "fresh"))
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
    channel = sub.add_parser("masters-channel").add_subparsers(dest="action", required=True)
    channel.add_parser("say").add_argument("text")
    channel.add_parser("read").add_argument("--all", action="store_true")
    culture = sub.add_parser("culture").add_subparsers(dest="action", required=True)
    culture.add_parser("set").add_argument("file")
    culture.add_parser("show")
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--to", default="")
    say.add_argument("--fyi", action="store_true")
    return parser


def _tick_store():
    if why := timer.installed_refusal():
        raise SwarmError(f"the tick refused to run: {why}")
    return outage.connect_tick(connect, now_ms)


def main(argv):
    if argv[:1] == ["init"]:
        from scripts.swarm import swarm_init

        return swarm_init.main(argv[1:])
    if argv[:2] == ["names", "check"]:
        from scripts.swarm import swarm_init

        return swarm_init.check(argv[2:])
    return dispatch(argv)


def typed(argv):
    """The agentihooks swarm command line, where create is refused for every swarm not named by code."""
    if argv[1:2] == ["create"] and not naming.is_code_named_swarm(argv[0]):
        print(f"swarm: a swarm is created with its ledger by one command: {naming.SWARM_INIT}", file=sys.stderr)
        return 1
    return main(argv)


def dispatch(argv):
    if argv and argv[0] in TOP_LEVEL:
        handler, args = globals()[f"cmd_{argv[0].replace('-', '_')}"], argparse.Namespace()
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
    from redis.exceptions import RedisError

    try:
        store = _tick_store() if handler in (cmd_tick, cmd_waits) else connect()
        if store is None:
            return 0
        if "slug" in args:
            args.slug = store.names.swarm_slug(args.slug)
        action = getattr(args, "command", "")
        route_slug = getattr(args, "slug", "")
        with ledger_route.command(store, route_slug, who):
            before = (
                control_notifications.master(store, args.slug) if action in control_notifications.CONTROLS else None
            )
            if action in clearance.COMMANDS:
                run_control(store, args, who, handler)
            else:
                handler(store, args)
            if action in control_notifications.CONTROLS:
                control_notifications.notify(store, args, LedgerClient(), before, action)
    except (SwarmError, InboxError, RedisError) as exc:
        if handler is cmd_status and outage.show(args.slug, args.json):
            return 0
        return failed(args, who, exc)
    return 0


def failed(args, who, exc) -> int:
    """A say to agents that met a Redis outage is queued; any other failure is reported, a bare Redis error raised."""
    from redis.exceptions import RedisError

    if isinstance(exc, (Unreachable, RedisError)) and queued_say(args, who):
        return 0
    if isinstance(exc, RedisError):
        raise exc
    print(f"swarm: {exc}", file=sys.stderr)
    return 1
