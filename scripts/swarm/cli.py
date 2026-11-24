"""Run a swarm of Claude and Codex agents over a swarm ledger in herdr.

agentihooks swarm list | tick | templates
agentihooks swarm init NAME [--plan FILE] [--content FILE] [--repo DIR]
agentihooks swarm <id> create --repo DIR [--template NAME]
agentihooks swarm <id> start | pause | stop [--now] | status | probe | url
agentihooks swarm <id> close [--note TEXT] [--now] | reopen | remove
agentihooks swarm <id> snapshot | shutdown | restore [--from FILE]
agentihooks swarm <id> take-master [--replace] | master up | master move --to HIVE
agentihooks swarm <id> set KEY=VALUE | autoscale | verdict | freeze | focus | unfreeze
agentihooks swarm <id> learned [--all]  caller notes and shared guidance; --all lists every seat
agentihooks swarm <id> learned TEXT [--maturity data|note|insight|canon]
agentihooks swarm <id> promote SEAT NUMBER insight|canon --reason TEXT | retire SEAT NUMBER --reason TEXT
agentihooks swarm <id> culture set FILE | culture show
Agent identity comes from --as or AGENTIHOOKS_AGENT_NAME.
Agent work: issue URL | pr URL | branch | publish | review | done | block NOTE | progress | wait
Continuity: handoff DOC | park DOC | ready DOC | restack | confirm-handoff | restore-decision
Use each command's --help for its arguments.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Iterable, MutableMapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import Token, copy_context
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import scripts.tick.steps.report  # noqa: F401
from hooks.classifier import settings as classifier_settings
from hooks.context import injection_trace, quarantine
from scripts import review
from scripts.doctor import priming
from scripts.forge.remote import CHANGE_LINK
from scripts.gates import Who, catalog, intent, lift, modes, quiet
from scripts.gates import feature as feature_gate
from scripts.gates import log as gate_log
from scripts.gates.identity import refusal
from scripts.handoff import check as handoff_check
from scripts.handoff import contracts as handoff_contracts
from scripts.handoff import envelope as handoff_envelope
from scripts.handoff import transfers
from scripts.handoff.resolve import Resolver
from scripts.hive import scope
from scripts.inbox import exits, outbox
from scripts.inbox import relay as relay
from scripts.inbox import wake as wake
from scripts.inbox.seats import CANON, SeatError, is_seat, seat_address
from scripts.inbox.seats import DEFAULT_MATURITY as DEFAULT_MATURITY
from scripts.inbox.seats import MATURITIES as MATURITIES
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import (
    affinity,
    agent_up,
    blockers,
    bottleneck,
    bottleneck_status,
    claim_lost,
    clearance,
    control_notifications,
    default_models,
    delivery,
    dev_red,
    dispatch_seat,
    doc_report,
    done_gate,
    enhance_review,
    expert_commands,
    expert_launch,
    failed_panes,
    git_https,
    handoff_commands,
    hive_release,
    idle,
    launch_check,
    ledger_events,
    linked_issues,
    master_launch,
    master_move,
    master_queue,
    master_refusals,
    merge_queue,
    naming,
    outage,
    overlays,
    panic,
    phase_planning,
    phase_state,
    phases,
    plan_cap_floor,
    plan_review,
    prerequisite_wait,
    priming_trace,
    prompt,
    pull_head,
    queue_priority,
    queue_record,
    ready,
    reaper,
    reboot,
    remote_control,
    review_rounds,
    run_window,
    seat_crons,
    seat_heads,
    snapshot,
    stack,
    stages,
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
from scripts.swarm import ledger_client as ledger_client
from scripts.swarm import (
    queue_exit as queue_exit,
)
from scripts.swarm import refresh_job as refresh_job
from scripts.swarm import snapshot_job as snapshot_job
from scripts.swarm.cli_parser import build_parser
from scripts.swarm.health import activity, focus_check, report
from scripts.swarm.health import findings as health
from scripts.swarm.ledger_client import FreezeWords, LedgerClient, LedgerGone, LedgerRefused
from scripts.swarm.pull_head import PULL_SECONDS as PULL_SECONDS
from scripts.swarm.pull_head import PullHead, pull_branch
from scripts.swarm.runtime import REPO_VARIABLE, HerdrRuntime, deferring
from scripts.swarm.status import (
    agent_hive,
    auto_snapshot,
    findings,
    live_agents,
    readable_hives,
    section_lines,
    status_report,
    task_counts,
    transport,
    verdict_store,
    wait_label,
)
from scripts.swarm.store import (
    APP_SERVER_TRANSPORT,
    ASSIST,
    AUTO_SCALING,
    AUTONOMY,
    DEFAULT_MASTERS_STEP,
    DELEGATE,
    DISPATCH,
    EVENTS_ENGINE,
    FULL,
    MASTER,
    AgentRecord,
    RedisStore,
    SwarmConfig,
    SwarmError,
    Unreachable,
    connect,
)
from scripts.swarm.tick import agent_status, primed, run_group, skip_refused, spawn_holds, tick
from scripts.swarm_ledger import (
    ledger_creator,
    ledger_kinds,
    ledger_link,
    ledger_route,
    ledger_workspace,
    plan_shape,
    workflow_task,
)
from scripts.swarm_v2 import front_door, master_scale, masters
from scripts.swarm_v2.runtime.routed import routed
from scripts.tick import engine, hook_reads, pool
from scripts.tick import registry as step_registry
from scripts.tick import view as ledger_view
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.adapters.host import local_host
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.adapters.ledger import ClientLedger, PassLedger, resource_client
from scripts.tick.adapters.llm import ApiLlm
from scripts.tick.adapters.metrics import SpoolMetrics
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.adapters.runtime import RouterRuntime
from scripts.tick.deadline import Deadline
from scripts.tick.ports.tools import PASS_TOOLS, Tools
from scripts.tick.sites import TICK_HOOK_KEYS, TICK_SHUT_KEYS, TICK_SITES, TickSites, run_sites
from scripts.tick.step import Domain
from scripts.tick.steps import Left, PassBox, budgets, hook_domains, pass_domains
from scripts.tick.steps import actions as actions_of
from scripts.tick.steps import host as host_steps
from scripts.tick.steps import report as report_steps
from scripts.tick.steps import tasks as task_steps

SETTABLE = {
    "max-eng-agents": "max_eng",
    "max-ci-agents": "max_ci",
    "max-plan-agents": "max_plan",
    "max-review-agents": "max_review",
    "masters-step": "masters_step",
    "compact-limit": "compact_limit",
    "snapshot-minutes": "snapshot_minutes",
    "master-move-grace": "master_move_grace",
    "local-max-agents": "local_max_agents",
    "focus-weight": "focus_weight",
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
LAB_URL = "lab-url"
ADDRESS_KEYS = {LAB_URL: "lab_url"}
TOP_LEVEL = ("list", "tick", "templates", "rename", "waker", "prober", "waits", "tick-watch", "spoke")
NO_LAB_ADDRESS = "lab-url takes an http or https address with a host and no credentials"
TRANSPORT_KEYS = {"codex-inbox-transport": "codex_inbox_transport"}
HIVE_PLACEMENT = "hive-placement"
ON_OFF = {"on": True, "off": False}
PLACEMENT_NODES = "placement-nodes"
WRAPPER_PREFIX = (
    "wrapper probes",
    "wrapper health",
    "wrapper commands",
    "wrapper phases",
    "wrapper refresh",
    "wrapper intent",
    "wrapper reboot",
)
PASS_SITES = (*WRAPPER_PREFIX, *TICK_SITES, "wrapper relay", "wrapper view", "wrapper events", "wrapper metrics")
NONE: frozenset[Domain] = frozenset()
WRAPPER_HOOK_KEYS: dict[str, tuple[frozenset[Domain], frozenset[Domain]]] = {
    "wrapper probes": (NONE, NONE),
    "wrapper commands": (pass_domains("stop"), NONE),
    "wrapper phases": (pass_domains("stop"), hook_domains("doc")),
    "wrapper intent": (pass_domains("stop"), hook_domains("doc", "config")),
    "wrapper relay": (pass_domains("judged"), NONE),
    "wrapper view": (pass_domains("agents", "stop"), hook_domains("agents", "doc", "config", "rows")),
    "wrapper metrics": (NONE, hook_domains("count")),
}
PASS_HOOK_KEYS = {**WRAPPER_HOOK_KEYS, **TICK_HOOK_KEYS}
PASS_SHUT_KEYS = {site: pass_domains("stop") | TICK_SHUT_KEYS.get(site, NONE) for site in PASS_SITES[1:]}
HEALTH_SITES = ("wrapper health", "wrapper commands")
WRAPPER_HOOK_READS = {"wrapper relay": frozenset({"judged"}), "wrapper view": frozenset({"agents"})}
ENDS_AFTER = ("wrapper probes", *HEALTH_SITES)
LEDGER_HOOKS = frozenset({"wrapper commands", "wrapper phases", "wrapper intent", "wrapper view"})
CONFIG_KEYS = {
    **SCALING_KEYS,
    **ADDRESS_KEYS,
    **default_models.KEYS,
    **TRANSPORT_KEYS,
    HIVE_PLACEMENT: "hive_placement",
    "engine": "engine",
    "tick-mode": "tick_mode",
    "watchdog-address": "watchdog_address",
    "placement-mode": "placement_mode",
    PLACEMENT_NODES: "placement_nodes",
}
GATE_KEYS = {f"{name}-gate": name for name in catalog.defaults()}
RETIRES_MASTER = frozenset({"stop now", "close ledger"})
TICK_LOCK_MS = 10 * 60 * 1000
# Every swarm ticks in a thread of one process; at the default 5 ms a CPU heavy swarm stalls the others' round trips
TICK_SWITCH_INTERVAL_S = 0.0005
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
WEB_REPO = re.compile(r"[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:|([\w-]+\.)+(com|org|net|io|dev)/", re.IGNORECASE)
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


def _publish_tick_status(
    store: Any,
    slug: str,
    doc: Any,
    view: Any = None,
    failure: BaseException | None = None,
    found: list[dict[str, Any]] | None = None,
) -> None:
    """Publish the tick status, reusing ``found`` health findings, and raise a publish error only for a passing tick."""
    from scripts.swarm import command_runner

    try:
        timing.call(command_runner.publish, store, slug, doc, view, found)
    except Exception:
        if failure is None:
            raise


def _phase_steps(inbox: Any, store: Any, slug: str, doc: dict[str, Any], ledger: Any) -> list[Any]:
    actions = skip_refused(phase_planning.planning_pass, inbox, store, slug, doc, ledger, store.config(slug))
    if actions:
        doc = timing.call(ledger_client.pass_state, ledger, slug)
    return actions + task_steps.phase_writes(skip_refused(phases.phase_changes, doc))


def _planning_after_phase(inbox: Any, store: Any, slug: str, ledger: Any, box: dict[str, Any]) -> list[str]:
    if task_steps.PHASE_CHANGED not in box:
        return []
    del box[task_steps.PHASE_CHANGED]
    doc = timing.call(ledger_client.pass_state, ledger, slug)
    return skip_refused(phase_planning.planning_pass, inbox, store, slug, doc, ledger, store.config(slug))


def _bin_stop(store, slug, ledger, runtime):
    store.redis.set(store.key(slug, "ledger-binned"), 1)
    _, left = stop_now(store, slug, runtime, ledger)
    return [
        f"the ledger is in the bin, still retiring {', '.join(left)}" if left else "the ledger is in the bin, stopped"
    ]


def _hold_view(beating: ExitStack, slug: str, ledger: LedgerClient) -> None:
    """Hold the pass's ledger view until beating closes, started with one delta read under a short timeout when the
    client reads it, read again under the export budget when that read timed out without a fresh stored view; a
    slow or stopped server leaves the pass on the last good view this host stored, writing to
    the outbox and refusing fresh reads, and an address the credential may not travel to leaves it unheld for its
    ledger health probe."""
    beating.enter_context(ledger_view.holding())
    if not ledger_view.reads(ledger):
        return
    beating.callback(ledger_client.OFFLINE.set, False)
    beating.callback(ledger_client.SLOW.set, False)
    try:
        ledger_view.start(slug, ledger_client.SNAPSHOT_TIMEOUT_S)
    except ledger_link.Cleartext as exc:
        print(f"ledger {slug} view not held: {exc}", file=sys.stderr)
    except OSError as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
            raise
        _read_long_or_hold_stored(slug, ledger, exc)


def _read_long_or_hold_stored(slug: str, ledger: LedgerClient, exc: OSError) -> None:
    """Read the view of ledger ``slug`` again under the export budget when the short read timed out and this host
    stores no view fresh enough to run from, else hold the stored view."""
    if ledger_view.timed_out(exc) and not ledger_view.fresh(slug):
        try:
            ledger_view.start(slug, ledger_client.EXPORT_TIMEOUT_S)
            return
        except OSError as again:
            if isinstance(again, urllib.error.HTTPError) and again.code < 500:
                raise
            exc = again
    _hold_stored(slug, exc)


def _hold_stored(slug: str, exc: OSError) -> None:
    """Hold the last good view of ledger ``slug`` for the pass and run it offline, or leave the pass to its ledger
    health probe when this host stores none; a read that only timed out leaves its writes sent to the server first."""
    if ledger_view.hold_stored(slug) is None:
        print(f"ledger {slug} not answering ({exc}) and no view is stored to run from", file=sys.stderr)
        return
    print(f"ledger {slug} not answering ({exc}): the pass runs from its last good view", file=sys.stderr)
    ledger_client.OFFLINE.set(True)
    ledger_client.SLOW.set(ledger_view.timed_out(exc))


def _stopped_tick(store: RedisStore, slug: str, ledger: Any, held: Any) -> tuple[list[str], dict[str, Any]]:
    from scripts.swarm import command_runner, controller

    fenced = _fenced(held, controller.deployment_for(store.config(slug), os.environ))
    controls = timing.call(command_runner.consume, store, slug)
    if timing.call(ledger.binned, slug):
        return controls + _bin_stop(store, slug, ledger, fenced), {}
    doc = timing.call(ledger_client.pass_state, ledger, slug)
    store.redis.delete(*(store.key(slug, mark) for mark in tick_heartbeat.HALTS))
    return controls + timing.call(tick, slug, store, ledger, fenced, now_ms()), doc


def _intent_pass(inbox: Any, store: Any, slug: str, doc: dict, ledger: Any, config: Any) -> tuple[Future, list[str]]:
    """Start the pass view off the step and judge intent from one batched read of every watched pull request,
    returning the pending view and the check's actions."""
    building = ThreadPoolExecutor(max_workers=1)
    view = building.submit(copy_context().run, timing.call, ledger_events.tick_view, inbox, store, slug, doc)
    building.shutdown(wait=False)
    mode = intent.mode_of(config)
    urls = intent.watched_urls(slug, doc, mode)
    pulls = intent.Pulls(lambda batch: timing.call(ledger_events.pull_texts, batch), urls)
    check = intent.Check(
        slug,
        mode,
        now_ms(),
        ledger,
        ledger_events.Mail(inbox, store, slug),
        intent.pr_view,
        intent.judged,
        head=pulls.head,
        named=intent.named_view,
        text=pulls.text,
        merged=pulls.merged,
        draft=pulls.draft,
        limit=intent.JUDGED_PER_PASS,
        judging=intent.TICK_JUDGING,
    )
    return view, skip_refused(check.run, doc)


def _unstopped(owner: Any, hook: Callable[[], bool]) -> Callable[[], bool]:
    """Return ``hook`` answering false without running once the ledger health probe stopped the pass of ``owner``."""
    return lambda: "stop" not in owner.box and hook()


@dataclass
class _Wrapper:
    """The wrapper's sites of a pass graph: the checks the hand written wrapper ran between its groups, and the
    actions it returned in their order."""

    held: Any
    beating: ExitStack
    state: dict[str, Any]
    box: PassBox
    ticks: TickSites
    watching: ExitStack = dataclass_field(default_factory=ExitStack, init=False)
    seen: dict[str, Any] = dataclass_field(default_factory=dict[str, Any], init=False)
    ended: list[str] | None = dataclass_field(default=None, init=False)
    checked: list[str] = dataclass_field(default_factory=list[str], init=False)
    judging: PassBox = dataclass_field(init=False)
    groups: dict[str, str] = dataclass_field(init=False)

    @property
    def store(self) -> RedisStore:
        """Return the swarm store of the pass state."""
        return self.state["store"]

    @property
    def slug(self) -> str:
        """Return the swarm of the pass state."""
        return self.state["slug"]

    @property
    def ledger(self) -> Any:
        """Return the ledger client of the pass state."""
        return self.state["ledger"]

    def __post_init__(self) -> None:
        """Hand the intent check its own box, sharing the pass's left steps, and end the tick's scan with the watch."""
        self.judging = PassBox(left=self.box.left)
        self.state["judging"] = self.judging
        self.groups = {step.name: step.group for step in step_registry.steps()}
        self.watching.callback(self.ticks.close)

    def hooks(self) -> dict[str, Callable[[], bool]]:
        """Return the checks run before the wrapper's probes, commands, phases, intent, relay, view and metrics
        sites. The runtime is fenced at the first site, because the health site's steps run in the slow lane and a
        later site's step may spawn before its hook runs. Each check reads and writes the keys
        ``WRAPPER_HOOK_KEYS`` declares for its site; the checks that reach the ledger answer false without running
        once the ledger health probe stopped the pass, because the sites they open are shut then."""
        found = {
            "wrapper probes": self._fence,
            "wrapper commands": self._watch,
            "wrapper phases": self._read,
            "wrapper intent": self._judge,
            "wrapper relay": self._judged,
            "wrapper view": self._reread,
            "wrapper metrics": self._count,
        }
        guarded = hook_reads.guarded({site: found[site] for site in WRAPPER_HOOK_KEYS}, WRAPPER_HOOK_READS)
        return {site: _unstopped(self, hook) if site in LEDGER_HOOKS else hook for site, hook in guarded.items()}

    def shut(self, step: Any) -> bool:
        """Return whether ``step`` stays shut: past the probes once the ledger health probe stopped the pass unless it
        needs only Redis and the host, from the phases once their hook found the ledger binned or gone, so the probes,
        health and commands sites the barrier ran before that hook keep running, and as the tick's sites say, asked
        first so a step needing a step that raised falls whatever else shuts it."""
        ticked = self.ticks.shut(step)
        open_past_stop = step.group == "wrapper probes" or step.name in host_steps.REDIS_ONLY
        stopped = not open_past_stop and "stop" in self.box
        ended = self.ended is not None and step.group not in ENDS_AFTER
        return ticked or stopped or ended

    def actions(self, records: Iterable[Any]) -> list[str]:
        """Return the pass actions in the hand written order: health, commands, then the counted actions, then metrics."""
        found = tuple(records)
        return self._acted(found, HEALTH_SITES) + self._counted(found) + self._acted(found, ("wrapper metrics",))

    def _acted(self, records: Iterable[Any], sites: tuple[str, ...]) -> list[str]:
        """Return the actions of the records of steps at ``sites``, in record order."""
        return actions_of(record for record in records if self.groups.get(record.name) in sites)

    def _counted(self, records: tuple[Any, ...]) -> list[str]:
        """Return the actions metrics counts: phases and reboot, the tick unless it left a value, intent and events."""
        tick = [] if self.ticks.left else self._acted(records, TICK_SITES)
        early = self._acted(records, ("wrapper phases", "wrapper reboot"))
        return early + tick + self.checked + self._acted(records, ("wrapper events",))

    def _fence(self) -> bool:
        """Fence the runtime the rest of the pass uses."""
        from scripts.swarm import controller

        deployment = controller.deployment_for(self.store.config(self.slug), os.environ)
        self.state["runtime"] = _fenced(self.held, deployment)
        return True

    def _watch(self) -> bool:
        """Watch the rest of the pass for spawn stalls, until the graph returns."""
        from scripts.swarm.health import spawn_stall

        watched = spawn_stall.watch(self.store, self.slug, self.ledger, now_ms, self.state["runtime"])
        self.watching.enter_context(watched)
        return True

    def _read(self) -> bool:
        """End the pass when the ledger is binned or gone, else read its pass state, record its open fixer branches for
        the panic sweep and clear the halt marks."""
        store, slug, ledger = self.store, self.slug, self.ledger
        if timing.call(ledger.binned, slug):
            self.ended = _bin_stop(store, slug, ledger, self.state["runtime"])
            return False
        try:
            self.box["doc"] = timing.call(ledger_client.pass_state, ledger, slug)
        except LedgerGone as exc:
            first = store.redis.set(store.key(slug, "ledger-gone"), 1, nx=True)
            self.ended = (
                [f"{exc}; agentihooks swarm remove {slug} clears this swarm once it has no agents"] if first else []
            )
            return False
        panic.keep_fixers(store, slug, self.box["doc"]["tasks"])
        store.redis.delete(*(store.key(slug, mark) for mark in tick_heartbeat.HALTS))
        return True

    def _judge(self) -> bool:
        """Read the pass state and config the intent check judges against."""
        self.judging["doc"] = timing.call(ledger_client.pass_state, self.ledger, self.slug)
        self.judging["config"] = self.store.config(self.slug)
        return True

    def _judged(self) -> bool:
        """End the tick's scan and keep the intent check's checked actions when it stored them, leaving its view to
        the view wait step; an intent check that raised or overran its budget stored neither and leaves the pass
        going, and only the pass end raises what it held."""
        self.ticks.close()
        if "judged" in self.judging:
            _, self.checked = self.judging["judged"]
        return True

    def _reread(self) -> bool:
        """List the agents when the lane dropped the page relay, then read the pass state, config and task rows again
        for the view, events and metrics sites."""
        if "agents" not in self.box:
            self.box["agents"] = host_steps.listed(self.store, self.slug)
        doc = timing.call(ledger_client.pass_state, self.ledger, self.slug)
        self.box.update(doc=doc, config=self.store.config(self.slug), rows={t["id"]: t for t in doc["tasks"]})
        return True

    def _count(self) -> bool:
        """Hand metrics the number of actions the pass counted so far."""
        self.box["count"] = len(self._counted(tuple(self.seen.values())))
        return True


class _PassSpool:
    """The local metrics spool of one tick pass, opened on its first append."""

    def __init__(self) -> None:
        """Start with the spool closed."""
        self._box: Any = None

    def append_many(self, batches: list[tuple[Any, list[dict[str, object]]]], checkpoint: Callable[..., None]) -> None:
        """Open the spool on first use and append every batch in one transaction."""
        from scripts.swarm import metrics_outbox

        spool: Any = metrics_outbox
        sink = spool.settings(os.environ)
        if sink is None:
            return
        if self._box is None:
            self._box = spool.Outbox(spool.spool_path(), sink)
        self._box.append_many(batches, checkpoint)


def _fencing(store: RedisStore, slug: str) -> Callable[[object, str, Any], Any]:
    """Return the fence the runtime port of swarm ``slug`` puts its pass runtime behind: the lease held, in a
    deployment."""

    def fence(held: object, deployment: str, runtime: Any) -> Any:
        from scripts.swarm import controller

        return controller.FencedRuntime(store, slug, held, runtime, deployment)

    return fence


def _fenced(held: object, deployment: str) -> Any:
    """Fence the pass runtime to the lease ``held`` through the runtime port of the pass tools and return it."""
    tools: Any = PASS_TOOLS.get()
    return tools.runtime.fence(held, deployment, deadline=Deadline.after(TICK_LOCK_MS / 1000))


def _pass_tools(store: RedisStore, slug: str, owner: str, ledger: Any = None, runtime: Any = None) -> Tools:
    """Build the port tools of one tick pass of swarm ``slug`` from the real adapters, acting as ``owner``."""
    from scripts.swarm import dispatcher

    swarm: Any = store
    settings: Any = classifier_settings.load()

    def agent(name: str) -> Any:
        return next((record for record in swarm.agents(slug) if record.name == name), None)

    return Tools(
        ledger=ClientLedger(slug, resource_client(slug)) if ledger is None else PassLedger(slug, ledger),
        coordination=StoreCoordination(store, slug, owner),
        source_control=GitHubSourceControl("", Path.cwd(), "dev"),
        runtime=RouterRuntime(routed(os.environ).router, agent, legacy=runtime, fencing=_fencing(store, slug)),
        processes=PassProcesses(),
        inbox=StoreInbox(InboxStore(swarm.redis), f"tick@{slug}"),
        llm=ApiLlm(settings.models[0], settings.url),
        metrics=SpoolMetrics(_PassSpool(), {dispatcher.TABLE.name: dispatcher.TABLE}),
        host=local_host(store, slug, os.environ),
    )


def _end_hold(store: RedisStore, slug: str, token: str, keeping: Token[Any], revoke: Callable[[], None]) -> None:
    """Revoke the pass's step hook, release its tick lock, then drop the hook and stop hold even when the release
    raises."""
    from scripts.swarm import controller

    revoke()
    try:
        controller.release_tick_lock(store, slug, token, "pass")
    finally:
        timing.BEFORE_STEP.reset(keeping)
        tick_stop.drop(token)


@timing.instrument_tick
@scope.one_pass()
@outage.guard_tick
@ledger_route.tick
def run_tick(
    store: RedisStore,
    slug: str,
    ledger: LedgerClient | None = None,
    runtime: object | None = None,
    messenger: object | None = None,
    scheduled: bool = False,
) -> list[str]:
    from scripts.swarm import commands, controller, lease, tick_activity

    if not ledger_link.local_mode(os.environ) and not scheduled:
        return [CONTROLLER_TICKS]
    tick_stop.check()
    ledger = ledger or LedgerClient()
    outbox.flush(InboxStore(store.redis))
    runtime = runtime or routed(herdr=deferring(HerdrRuntime()))
    held = lease.acquire(store, slug, commands.hive_id())
    if held is None:
        return ["the swarm belongs to another hive"]
    token = controller.take_tick_lock(store, slug, held, TICK_LOCK_MS, "pass")
    if token is None:
        return ["another tick is running"]
    tick_stop.hold(token, lambda: controller.release_tick_lock(store, slug, token, "pass stop"))

    renewal = tick_activity.Renewal(
        store, slug, lambda: controller.keep_tick(store, slug, held, token, TICK_LOCK_MS, "pass step"), now_ms
    )
    keeping = timing.BEFORE_STEP.set(renewal)
    doc, view, beating, box, ended = {}, None, ExitStack(), PassBox(), None
    wrapper: _Wrapper | None = None
    failed: BaseException | None = None
    try:
        beating.enter_context(lease.held_pass(store, slug, held))
        beating.callback(PASS_TOOLS.reset, PASS_TOOLS.set(_pass_tools(store, slug, held.owner, ledger, runtime)))
        beating.enter_context(
            pool.running(
                mode=store.config(slug).tick_mode,
                heartbeat=lambda: controller.keep_tick(store, slug, held, token, TICK_LOCK_MS, "pass heartbeat"),
            )
        )
        timing.keep()
        budgets.resolve(store, slug)
        run_group("before")
        if store.config(slug).state == "stopped":
            _hold_view(beating, slug, ledger)
            actions, doc = _stopped_tick(store, slug, ledger, held)
            return actions
        state = {"store": store, "slug": slug, "runtime": runtime, "box": box, "ledger": ledger}
        state |= {"inbox": InboxStore(store.redis), "scheduled": scheduled}
        ticks = TickSites(state, box, task_steps.appliers(state), now_ms, scanning=True)
        wrapper = _Wrapper(held, beating, state, box, ticks)
        _hold_view(beating, slug, ledger)
        gate = engine.SiteGate(wrapper.hooks() | ticks.hooks(), wrapper.shut, wrapper.seen, sites=PASS_SITES)
        try:
            records = run_sites(PASS_SITES, state, box, gate, ticks.apply, PASS_HOOK_KEYS, PASS_SHUT_KEYS)
        finally:
            wrapper.watching.close()
        doc, view = box.get("doc", doc), host_steps.settled_view(box.get("view"))
        try:
            host_steps.raise_held(box)
        finally:
            host_steps.raise_held(wrapper.judging)
        if "stop" in box or wrapper.ended is not None:
            return box.get("stop") or wrapper.ended or []
        doc = timing.call(ledger_client.pass_state, ledger, slug)
        ended = report_steps.FULL
        return wrapper.actions(records)
    except Left as missing:
        ended = report_steps.EARLY
        return report_steps.early_end(store, slug, missing, box.left)
    except (Exception, ledger_link.Cleartext) as exc:
        failed = exc
        return host_steps.held_failure(exc, box, getattr(wrapper, "judging", {}))
    finally:
        try:
            published: dict[str, Any] = PassBox()
            closing: dict[str, Any] = {
                "store": store,
                "slug": slug,
                "doc": doc,
                "view": view,
                "box": published,
                "ended": ended,
            }
            run_group(report_steps.CLOSING_GROUP, **closing, failure=sys.exception() or failed)
            host_steps.end_held(published)
        finally:
            try:
                beating.close()
            finally:
                _end_hold(store, slug, token, keeping, renewal.revoke)


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


def _reconcile_services():
    try:
        installed = timer.reconcile(timer.entry_point())
    except (OSError, subprocess.SubprocessError) as exc:
        installed, why = False, f": {exc}"
    else:
        why = ""
    if not installed:
        print(f"warning: the swarm user services could not be installed or enabled{why}", file=sys.stderr)


def run_waits(store: RedisStore, slug: str, ledger=None) -> list[str]:
    from scripts.swarm_ledger import authority_context

    if ledger_link.remote(slug=slug) and not authority_context.tick_headers(slug):
        return []
    return _end_waits(store, slug, ledger)


def _end_waits(store: RedisStore, slug: str, ledger=None) -> list[str]:
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
    doc = ledger_client.pass_state(ledger, slug)
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

    def check(job: tuple[str, Callable[[RedisStore, str], list[str]]]) -> None:
        slug, passing = job
        try:
            for action in passing(store, slug):
                timing.emit(sys.stdout, f"{slug}: {action}")
        except Exception as exc:
            timing.emit(sys.stderr, f"{slug}: {type(exc).__name__}: {exc}")

    jobs = [(slug, run_waits) for slug in store.slugs()]
    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            list(pool.map(check, jobs))


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
    from scripts.swarm_ledger.api import client as ledger_api

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
            with ledger_api.kept_exports():
                _tick_host(store, started)
    except scheduled_tick.AlreadyRunning:
        print("another swarm tick is running on this machine", file=sys.stderr)


def cmd_swarm_tick(store: RedisStore, args: argparse.Namespace) -> None:
    """Tick one swarm on this workstation, refused for a swarm the Anton controller serves."""
    from scripts.swarm import scheduled_tick

    if scheduled_tick.served(store.config(args.slug)):
        raise SwarmError(scheduled_tick.SERVED.format(slug=args.slug))
    for action in run_tick(store, args.slug):
        print(action)


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
    sys.setswitchinterval(TICK_SWITCH_INTERVAL_S)
    with authority_context.for_tick(store) as slugs:
        _reconcile_services()
        from hooks.context import documentation_session

        documentation_session.schedule_retry()
        if slugs:
            if tick_stop.run(slugs, lambda slug: _tick_one(store, slug), time.monotonic() - started):
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

    quota_refresh.run(
        store,
        lambda slug, row: command_runner.execute(store, slug, row),
        quota_refresh.WAIT_S,
        lambda: quota_refresh.probe(dict(os.environ), time.time()),
    )


def cmd_spoke(store, args):
    from scripts.events import factory
    from scripts.events.config import BusConfig
    from scripts.hive import lanes, workstation
    from scripts.swarm import commands
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    if workstation.runs_engine(os.environ):
        raise SwarmError(workstation.ENGINE_RUNS)
    lanes.serve(os.environ)
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

        def repair(slug: str, now: int) -> list[str]:
            return tick_watch.bounded(
                lambda: place_master(slug, store, runtime, now), tick_watch.REPAIR_S, "the master placement"
            )

    tick_watch.run(store, now_ms, repair)


def create_config(args: argparse.Namespace) -> SwarmConfig:
    from scripts.hive.daemon import origin

    if not SLUG_RE.match(args.slug):
        raise SwarmError("a swarm id is lowercase letters, digits and dashes, starting with a letter, at most 48 long")
    if WEB_REPO.match(args.repo):
        raise SwarmError(f"--repo takes a folder on this host, not the web address {args.repo}")
    repo = os.path.abspath(os.path.expanduser(args.repo))
    template = templates.load(args.template, os.environ) if args.template else templates.parse({"name": "none"})
    caps = {key: value.cap for key, value in template.lanes.items()}
    return SwarmConfig(
        args.slug,
        repo,
        caps["eng"] if args.max_eng_agents is None else args.max_eng_agents,
        caps["ci"] if args.max_ci_agents is None else args.max_ci_agents,
        max_plan=caps["plan"] if args.max_plan_agents is None else args.max_plan_agents,
        max_review=caps["review"],
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
        engine=EVENTS_ENGINE,
        repo_origin=origin(Path(repo)),
    )


def cmd_create(store: RedisStore, args: argparse.Namespace) -> None:
    config = create_config(args)
    ledger = LedgerClient()
    tasks = ledger_creator.swarm_tasks(ledger.state(args.slug))
    refused = ledger_creator.swarm_refusal(os.environ) or (
        args.template != priming.TEMPLATE and ledger_creator.floor_refusal(os.environ, tasks, args.operator_asked)
    )
    refused = refused or store.refusal(config)
    if refused:
        raise SwarmError(refused)
    ledger.mark_swarm(args.slug)
    store.create(config)
    from scripts.swarm import commands

    commands.bind(store, args.slug, commands.hive_id())
    print(
        json.dumps(
            {
                "created": args.slug,
                "repo": config.repo,
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
    for action in getattr(args, "tick", run_tick)(store, args.slug):
        print(action)
    print(json.dumps({"swarm": args.slug, "state": store.config(args.slug).state}))


def cmd_start(store: RedisStore, args: argparse.Namespace) -> None:
    shape = plan_shape.report(LedgerClient().tasks(args.slug), store.config(args.slug).max_eng)
    print(shape["summary"], flush=True)
    if shape["warning"]:
        print(f"warning: {shape['warning']}", file=sys.stderr, flush=True)
    plan_cap_floor.restore(store, args.slug)
    _state(store, args, "running")
    seat_crons.on_start(store, args.slug)
    print(ledger_link.page_line(args.slug))


def cmd_url(store, args):
    print(ledger_link.page_line(args.slug))


def cmd_pause(store, args):
    _state(store, args, "paused")


def cmd_proof_control(store, args):
    from scripts.swarm_ledger import proof_ledger

    result = proof_ledger.control(args.slug, json.loads(Path(args.request).read_bytes()))
    print(json.dumps(result))


def cmd_stop(store: RedisStore, args: argparse.Namespace) -> None:
    _snapshot(store, args.slug)
    if not args.now:
        _state(store, args, "stopping")
        seat_crons.on_stop(args.slug)
        return
    config, left = stop_now(store, args.slug, routed(herdr=HerdrRuntime()), LedgerClient())
    seat_crons.on_stop(args.slug)
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


def _retire_each(store: RedisStore, slug: str, runtime: Any, agents: list[AgentRecord]) -> list[str]:
    left: list[str] = []
    for agent in agents:
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)
        if not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
            if not seat_heads.superseded(agent, agents):
                store.put_agent(slug, agent)
            left.append(agent.name)
    return left


def cmd_close(store: RedisStore, args: argparse.Namespace) -> None:
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
    seat_crons.on_stop(args.slug)
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


def on_ledger_host(slug: str, environ: MutableMapping[str, str] = os.environ) -> None:
    from scripts.swarm_ledger import hosted_probe, storage

    if environ.get(hosted_probe.HOSTED_ENV) and storage.default().token(slug):
        environ.pop(hosted_probe.HOSTED_ENV)


def cmd_master(store, args):
    if args.action == "move":
        print(json.dumps(master_move.order(store, args.slug, args.to, now_ms())))
        return
    on_ledger_host(args.slug)
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
    print(json.dumps(asdict(launched)))


def cmd_agent_up(store, args):
    launched = agent_up.up(store, args.slug, HerdrRuntime(), args.profile, now_ms())
    print(json.dumps(asdict(launched)))


def cmd_exit(store: RedisStore, args: argparse.Namespace) -> None:
    name = store.names.resolve(args.name or Who.from_env().name)
    agent = next((a for a in store.agents(args.slug) if a.name == name), None)
    expert_commands.seated(store, args.slug, agent)
    released = agent is not None and agent.lane != MASTER and agent.state == "finished"
    if agent and agent.lane != MASTER and not released and _claimless(store, args.slug, agent):
        _retire(store, args.slug, agent, "finished its session without a claim")
    elif not released:
        agent_up.retire(store, args.slug, name, now_ms())
    print(json.dumps({"exited": name}), flush=True)
    HerdrRuntime().reap_name(name)


def _claimless(store, slug, agent):
    from scripts.hive import scope

    if store.claimant(slug, agent.task) != agent.name:
        return True
    return claim_lost.lost(agent.name, LedgerClient().task(slug, agent.task), bool(scope.member()))


def gate_mode(key, value):
    value = modes.normalize(value)
    if value not in modes.supported(GATE_KEYS[key]):
        raise SwarmError(f"{key} takes {', '.join(modes.label(mode) for mode in modes.supported(GATE_KEYS[key]))}")
    return {GATE_KEYS[key]: value}


def setting(config, key):
    if key in GATE_KEYS:
        return modes.label(catalog.current(config.gates)[GATE_KEYS[key]])
    if key in run_window.KEYS:
        return run_window.reading(config.window, key)
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
            if key in (*SETTABLE, *LANE_KEYS, *EFFORT_KEYS, *CONFIG_KEYS, *GATE_KEYS, *run_window.KEYS, "autonomy")
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


def api_address(value: str, refusal: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise SwarmError(refusal) from None
    if value and (parts.scheme not in ("http", "https") or not host or "@" in parts.netloc):
        raise SwarmError(refusal)
    return value


def config_value(key, value):
    if (
        key in default_models.KEYS
        or key in TRANSPORT_KEYS
        or key in ("engine", "tick-mode", "watchdog-address", "placement-mode")
    ):
        return value
    if key == HIVE_PLACEMENT:
        return on_off(value)
    if key == PLACEMENT_NODES:
        if not value.isdigit():
            raise SwarmError("placement-nodes takes a whole number of hives, 0 for no limit")
        return int(value)
    if key == LAB_URL:
        return api_address(value, NO_LAB_ADDRESS)
    return scaling_value(key, value)


def _engine_origin(config, changes):
    if changes.get("engine") != "events" or config.repo_origin:
        return changes
    from scripts.hive.daemon import origin

    identity = origin(Path(config.repo))
    if not identity:
        raise SwarmError("events engine requires a checkout with a Git origin or a stored repository origin")
    return {**changes, "repo_origin": identity}


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


def cmd_set(store: RedisStore, args: argparse.Namespace) -> None:
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
        if key in run_window.KEYS:
            window = changes.get("window", store.config(args.slug).window)
            changes["window"] = run_window.setting(window, key, value, now_ms())
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
                f"effort-min=E, effort-max=E, claude-model=M, codex-model=M, scaling=auto|manual, load-high=N, load-low=N, memory-per-agent=MB, swap-full-share=N, swap-floor=MB, codex-inbox-transport=legacy|app_server "
                f"or {', '.join(LANE_KEYS)}=<value>, engine=events, tick-mode=sequential|parallel"
            )
        changes[SETTABLE[key]] = int(value)
    changes = _engine_origin(store.config(args.slug), changes)
    restored = plan_cap_floor.restored(store.config(args.slug), changes)
    config = store.update(args.slug, **{**changes, **_pinned(changes), **restored})
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
                "max_review": config.max_review,
                "masters_step": config.masters_step,
                "compact_limit": config.compact_limit,
                "focus_weight": config.focus_weight,
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
                "placement_mode": config.placement_mode,
                "placement_nodes": config.placement_nodes,
                "local_max_agents": config.local_max_agents,
                "api_url": config.api_url,
                "codex_inbox_transport": config.codex_inbox_transport,
                "engine": config.engine,
                "tick_mode": config.tick_mode,
                "window": run_window.report(config.window, now_ms()),
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


def probe_command(store, args):
    from scripts.swarm import probe

    for found in probe.run(store, args.slug, LedgerClient(), now_ms()):
        print(found.line())


def hive_admission(store: Any, slug: str) -> dict[str, Any]:
    """Each readable live hive's admission room and limit, and the binding limit against quota seats and host room."""
    from scripts.hive import admission
    from scripts.swarm import capacity

    hives, refused = readable_hives(store)
    record: dict[str, Any] = capacity.read(store, slug)
    host: dict[str, Any] = record.get("host") or {}
    environ = dict(os.environ)
    observed = capacity.accounts(environ, time.time(), refresh=False)
    seats = capacity.quota_seats(observed, capacity.warned_accounts(observed, environ))
    others: dict[str, int | None] = {"quota seats": seats, "host room": host.get("room")}
    rows = [{"hive": hive["id"], "room": hive["room"], "limit": hive["limit"]} for hive in hives]
    return {"hives": rows, "lines": admission.view_lines(hives, others, refused)}


def cmd_status(store: Any, args: Any) -> None:
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
        f"{naming.swarm_name(config.code) or '-'}  {config.slug}  {config.state}  eng {config.max_eng}  ci {config.max_ci}  plan {config.max_plan}  review {config.max_review}  masters step {config.masters_step}  scaling {config.scaling}  focus weight {config.focus_weight}  effort {config.effort_min} to {config.effort_max}  repo {config.repo}"
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

    quota = quota_view.lines(capacity.read(store, args.slug), now_ms(), quota_view.live(agents), config.scaling)
    held = spawn_holds(store, args.slug) + failed_panes.status_lines(store, args.slug)
    held += incidents.status_lines(store, args.slug)
    from scripts.hive import admission, placement_waits

    held += admission.status_lines(placement_waits.read(store, args.slug))
    held += master_refusals.status_lines(store, args.slug, now_ms())
    held += section_lines("hive admission", lambda: hive_admission(store, config.slug)["lines"])
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
        "Agent\tLane\tHarness\tProfile\tModel\tAccount\tPane\tTask\tState\tConversation\tPush inbox\tNative terminal\tModel source\tConfidence\tHive\tWait"
    )
    for a in agents:
        model = " ".join(filter(None, (a.model, a.effort))) if a.model else "unknown"
        can = transport(a)
        push = "yes" if can["push_inbox"] else "no"
        attach = "yes" if can["native_terminal_attach"] else "no"
        print(
            f"{a.name}\t{a.lane}\t{a.harness}\t{a.profile or 'unknown'}\t{model}\t{a.account or '-'}\t{a.pane_id}\t{a.task}\t{agent_status(a)}\t{a.conversation_id or '-'}\t{push}\t{attach}\t{a.model_source or '-'}\t{a.model_confidence if a.model_confidence is not None else '-'}\t{agent_hive(a) or '-'}"
            f"\t{wait_label(store, args.slug, a)}"
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
    sys.stdout.write(report.text(found, verdict_store(store, args.slug).false_positives()))
    for row in gate_log.decisions(args.slug):
        print(
            f"gate  {modes.label(row['kind'])}  {row.get('gate')}  {row.get('agent')}  {row.get('task')}  {row.get('reason')}"
        )


def cmd_freeze(store: RedisStore, args: argparse.Namespace) -> None:
    writer = clearance.freezer(store, args.slug, Who.from_env())
    by = None if writer == clearance.OPERATOR else writer
    full = store.config(args.slug).autonomy == FULL
    if by and not args.quote and not full:
        raise SwarmError(
            "below full autonomy the master writes freezes only with the operator's words: pass them with --quote"
        )
    given: dict[str, Any] = vars(args)
    queue = {key: given[key] for key in ("position", "only") if given.get(key)}
    words = FreezeWords(reason=args.reason or "", quote=args.quote or "", **queue)
    LedgerClient().freeze(args.slug, args.command, args.target, by=by, words=words)
    print(json.dumps({args.command: args.target, "by": writer, **queue}))


def cmd_cover(store, args):
    from scripts.swarm import stage_cover

    by = clearance.coverer(store, args.slug, Who.from_env())
    doc = LedgerClient().state(args.slug)
    request = stage_cover.Request(args.stage, args.item, args.reason, by)
    print(json.dumps(stage_cover.cover(store, args.slug, doc, request, now_ms())))


cmd_focus = cmd_unfreeze = focus_check.checked(cmd_freeze, lambda: LedgerClient())


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


def cmd_autoscale(store: RedisStore, args: argparse.Namespace) -> None:
    from scripts.hive import admission
    from scripts.swarm import capacity, local_slots

    config = store.config(args.slug) if args.slug in store.slugs() else SwarmConfig(args.slug, "", max_eng=0, max_ci=0)
    if args.fixture:
        inputs = capacity.fixture_inputs(json.loads(Path(args.fixture).read_text()))
    else:
        inputs = capacity.live_inputs(args.slug, store, LedgerClient(), dict(os.environ), now_ms())
    _, decision = capacity.autoscaled(replace(config, scaling=AUTO_SCALING), inputs, capacity.tick_host(inputs))
    known = args.slug in store.slugs()
    slots = {"local_slots": local_slots.maximum(store, args.slug, config.local_max_agents)} if known else {}
    host: dict[str, Any] = (decision or {})["host"]
    hives, refused = readable_hives(store) if not args.fixture else ([], {})
    admitted: list[str] = []
    if hives or refused:
        seats = capacity.inputs_seats(inputs)
        admitted = admission.view_lines(hives, {"quota seats": seats, "host room": host["room"]}, refused)
    if args.json:
        rows = [{"hive": hive["id"], "room": hive["room"], "limit": hive["limit"]} for hive in hives]
        shown: dict[str, Any] = {"hive_admission": rows, "binding": admitted[-1]} if hives else {}
        print(
            json.dumps(
                {
                    "scaling": config.scaling,
                    "applied": config.scaling == AUTO_SCALING,
                    **(decision or {}),
                    **slots,
                    **shown | ({"admission_unreadable": refused} if refused else {}),
                },
                indent=2,
            )
        )
        return
    local = [f"local slots {slots['local_slots']}, host room guard applies"] if known else []
    print("\n".join([*autoscale_lines(config, decision), *local, *admitted]))


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


def cmd_blockers(store: RedisStore, args: argparse.Namespace) -> None:
    if args.action == "clear":
        if not args.category:
            raise SwarmError("blockers clear needs a category")
        blockers.clear(store, args.slug, args.category)
        print(json.dumps({"cleared": args.category}))
        return
    found = sorted(blockers.active(store, args.slug, now_ms()).values(), key=lambda row: row.category)
    print(
        json.dumps(
            [
                {
                    "category": row.category,
                    "first_seen": row.first_seen,
                    "last_seen": row.last_seen,
                    "count": row.count,
                    "detail": row.detail,
                }
                for row in found
            ]
        )
    )


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    """The panic button: on, pressed by the master or the operator, stops agent starts, cancels CI runs and pauses the
    lab's pull request grading but the checked fixes and the fixer branches every swarm recorded; drop, by the master or
    the operator, removes one registered fix and tells the lab the fixes left; off clears it once stable and resumes the
    lab."""
    now = now_ms()
    if args.action == "drop":
        _master_or_operator(store, args, "only the master or the operator drops a panic fix")
        if len(args.fix) != 1:
            raise SwarmError("panic drop takes exactly one --fix")
        found = panic.drop(store, args.slug, args.fix[0])
        lab = panic.lab_engage(store, args.slug, found.fixes)
        print(json.dumps({"panic": "on", "fixes": list(found.fixes), "lab": lab}))
        return
    if args.action == "on":
        _master_or_operator(store, args, "only the master or the operator presses panic on")
        for fix in args.fix:
            panic.check_fix(fix)
        found = panic.engage(store, args.slug, f"pressed on swarm {args.slug}", False, args.fix, now)
        owns = panic.owner(store, args.slug)
        cancelled = panic.cancel_runs((*found.fixes, *panic.fixers(store)), panic.REPOS, panic.port, owns)
        lab = panic.lab_engage(store, args.slug, found.fixes)
        print(json.dumps({"panic": "on", "fixes": list(found.fixes), "cancelled": cancelled, "lab": lab}))
        return
    if args.action == "off":
        if not panic.release(store, args.slug, now):
            raise SwarmError(
                f"panic holds: business as usual is at {panic.level(store, args.slug, now):.2f} with "
                f"{panic.errors_since(store, args.slug, now - panic.WINDOW_MS)} errors in the last 15 minutes; it "
                f"clears at {panic.RELEASE_LEVEL:.2f} with fewer than {panic.RELEASE_ERRORS}"
            )
        print(json.dumps({"panic": "off", "lab": panic.lab_release(store, args.slug)}))
        return
    current = panic.state(store, args.slug)
    print(
        json.dumps(
            {
                "panic": "on" if current else "off",
                "since": current.since if current else None,
                "reason": current.reason if current else "",
                "fixes": list(current.fixes) if current else [],
                "errors_last_hour": panic.errors_by_category(store, args.slug, now - panic.HOUR_MS),
                "threshold": panic.THRESHOLD,
                "level": round(panic.level(store, args.slug, now), 3),
            }
        )
    )


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


def _worker(store: RedisStore, args: argparse.Namespace) -> AgentRecord:
    agent: AgentRecord = _me(store, args)
    if agent.lane == MASTER:
        raise SwarmError(f"{agent.name} is the swarm master; the master works no task")
    if agent.lane == expert_launch.LANE:
        raise SwarmError(f"{agent.name} is an expert; an expert works its mission and claims no task")
    return agent


def _local_repo(store, slug):
    local = os.environ.get(REPO_VARIABLE)
    return local if local and os.environ.get("AGENTIHOOKS_SWARM") == slug else store.config(slug).repo


def cmd_issue(store, args):
    agent = _worker(store, args)
    LedgerClient().update_task(args.slug, agent.task, {"issue_url": args.url}, by=agent.name)
    print(json.dumps({"task": agent.task, "issue_url": args.url}))


def cmd_resolve(store, args):
    from scripts.swarm import resolve

    resolve.run(store, args, _me(store, args).name, LedgerClient())


def cmd_review(store: RedisStore, args: argparse.Namespace) -> None:
    agent = _worker(store, args)
    print(json.dumps(review_rounds.start(args.slug, agent.task, LedgerClient(), agent.name)))


def _git(run, *args):
    try:
        return git_https.run(["git", *args], runner=run, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SwarmError(f"git {args[0]} could not run: {exc}") from exc


def worktree_branch(recorded: str | None, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str:
    current = _git(run, "branch", "--show-current")
    branch = current.stdout.strip() if current.returncode == 0 else ""
    if not branch:
        raise SwarmError("swarm branch runs in a worktree on a branch; this checkout is on none")
    target = f"HEAD:{recorded}" if recorded and recorded != branch else branch
    branch = recorded if recorded and recorded != branch and _upstream(run) == recorded else branch
    if _git(run, "ls-remote", "--exit-code", "--heads", "origin", branch).returncode == 0:
        return branch
    upstream = "" if recorded or (found := _upstream(run)) in ("dev", "main", "master", "v1") else found
    if not upstream or _git(run, "ls-remote", "--exit-code", "--heads", "origin", upstream).returncode != 0:
        raise SwarmError(f"branch {branch} is not on origin; push it first with git push -u origin {target}")
    return upstream


def _upstream(run: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    done = _git(run, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    return done.stdout.strip().partition("/")[2] if not done.returncode and done.stdout.startswith("origin/") else ""


def origin_repo(run=subprocess.run):
    done = _git(run, "remote", "get-url", "origin")
    return stack.public_url(done.stdout.strip()) if done.returncode == 0 else ""


def publication_command(store: RedisStore, args: argparse.Namespace) -> dict:
    import syslog

    from scripts.swarm import pr_wait, publish

    agent = _worker(store, args)
    repo = _local_repo(store, args.slug)
    ledger = LedgerClient()
    task = {}
    try:
        task = ledger.task(args.slug, agent.task) or {}
        if args.command == "prepare":
            return publish.prepare(args.slug, agent, repo, ledger)
        if args.direct and args.via != publish.LAB:
            raise SwarmError("publish --direct goes with --via lab")
        pushed = partial(pr_wait.lab_pushed, store.redis, args.slug, agent) if args.via == publish.LAB else None
        lab = publish.LabRoute(agent.task, args.direct, pushed) if args.via == publish.LAB else None
        return publish.publish(args.slug, agent, repo, ledger, args.message, args.files, lab)
    except SwarmError as exc:
        branch = exc.branch if isinstance(exc, publish.PublicationRefusal) else publish.refusal_branch(Path(repo), task)
        syslog.openlog("agentihooks-swarm")
        syslog.syslog(
            syslog.LOG_WARNING,
            json.dumps(
                {
                    "event": "swarm_publication_refused",
                    "slug": args.slug,
                    "command": args.command,
                    "task": agent.task,
                    "branch": branch,
                    "reason": str(exc),
                }
            ),
        )
        raise


def cmd_prepare(store, args):
    print(json.dumps(publication_command(store, args)))


def cmd_publish(store, args):
    print(json.dumps(publication_command(store, args)))


def cmd_branch(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    row = ledger.task(args.slug, agent.task) or {}
    branch = worktree_branch(row.get("branch"))
    fields = {"branch": branch, "branch_repo": origin_repo()}
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    carried = {"pr_url": row["pr_url"]} if row.get("pr_url") else {}
    print(json.dumps({"task": agent.task, **fields, **carried}))


def cmd_pr(store: RedisStore, args: argparse.Namespace) -> None:
    agent = _worker(store, args)
    config = store.config(args.slug)
    awaiting = "approval" if config.autonomy == ASSIST else ""
    ledger = LedgerClient()
    doc, row = _stamp_scope(ledger, args.slug, agent.task)
    keeps_main = args.prerequisite is not None and (row.get("pr_url") or args.url) != args.url
    head, repo, number, sha, body = PullHead(*pull_branch(args.url))
    pull_head.refuse_other_branch(row, args, PullHead(head, repo))
    if not keeps_main and not pull_head.gitlab_link(args.url):
        linked_issues.refuse_unnamed(args.url, body, row, lambda member: ledger.task(args.slug, member))
    branch = {"branch": head, "branch_repo": repo} if head and not keeps_main else {}
    confirmed = {"pr_number": number, "pr_head": sha} if number and not keeps_main else {}
    fields = {"state": "pr", "awaiting": awaiting, **branch, **confirmed} | ({} if keeps_main else {"pr_url": args.url})
    fields.update(_split_binding(row, args.url, args.half))
    scoped = _pull_scope(row, args)
    row.update(scoped)
    fields.update(scoped)
    checked = intent.stamp(args.slug, agent.task, args.url, doc, intent.mode_of(config), now_ms())
    drop_lifts = not keeps_main and row.get("pr_url") != args.url
    ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    if drop_lifts:
        lift.drop_task(args.slug, agent.task, store=store)
    print(json.dumps({"task": agent.task, "pr_url": args.url, **branch, "intent": checked}))


def _stamp_scope(ledger, slug, task_id):
    row = ledger.task(slug, task_id)
    if row.get("split"):
        doc = ledger.state(slug)
        return doc, next(t for t in doc["tasks"] if t.get("id") == task_id)
    phase = ledger.item(slug, f"phases/{row.get('phase')}") if row.get("phase") else None
    doc = {"overview": ledger.metadata(slug)["overview"], "phases": [phase] if phase else [], "tasks": [row]}
    return doc, row


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


def _pull_scope(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    scopes = row.get("pull_scopes") or {}
    kind = "stage" if args.stage is not None else "prerequisite" if args.prerequisite is not None else ""
    if not kind:
        return {"pull_scopes": {url: s for url, s in scopes.items() if url != args.url}} if args.url in scopes else {}
    scope = getattr(args, kind).strip()
    if not scope:
        raise SwarmError(f"--{kind} needs the scope this pull request delivers")
    if not CHANGE_LINK.fullmatch(args.url):
        raise SwarmError(f"--{kind} needs a GitHub pull request or GitLab merge request URL, not {args.url}")
    return {"pull_scopes": {**scopes, args.url: {"kind": kind, "scope": scope}}}


def _queue_intent(store, args, agent, head):
    state = intent.Verdicts(args.slug, intent.NAME)
    who = Who(name=agent.name, swarm=args.slug, task=agent.task)
    decision = intent.queue_decision(who, state, args.url, intent.mode_of(store.config(args.slug)), head)
    if not decision.allowed:
        gate_log.append(args.slug, gate_log.Row.of(intent.NAME, "deny", who, "Bash", decision.reason))
        raise SwarmError(decision.reason)


def _hotfix_master(store, args) -> bool:
    from hooks.context import master_coding

    return _me(store, args).lane == MASTER and master_coding.is_open(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))


def _require_target(store: object, args: argparse.Namespace, agent: AgentRecord) -> None:
    """Refuse a final integration the claimant's task does not name and the claimant did not open, reading only its
    own task and the tasks naming the pull request, never the whole tasks collection."""
    ledger = LedgerClient()
    own = ledger.task(args.slug, agent.task)
    rows: list[dict[str, object]] = [own] if own else []
    targeting = rows + ledger.tasks_targeting(args.slug, args.url)
    done_gate.require_target(store, args.slug, agent.task, args.url, targeting, agent.name)


def cmd_merge(store: Any, args: argparse.Namespace) -> None:
    from scripts.review import gate as review

    if args.action == "priority":
        sender = _master_or_operator(store, args, "only the master or the operator prioritizes the merge queue")
        tasks = LedgerClient().tasks(args.slug)
        if args.lift and not clearance.is_operator(store, Who.from_env()):
            raise SwarmError("only the operator lifts a running merge group priority hold")
        result = queue_priority.request(store, args.slug, args.url, sender, tasks, lift=args.lift)
    else:
        hotfix = args.action == "queue" and _hotfix_master(store, args)
        caller = _me(store, args) if args.action != "state" and not hotfix else None
        master = args.action == "queue" and caller is not None and caller.lane == MASTER
        stalled = master_queue.route(store, args.slug, args.url, LedgerClient()) if master else None
        hotfix = hotfix or stalled is not None
        tasked = caller is not None and not hotfix and caller.lane != expert_launch.LANE
        agent = _worker(store, args) if tasked else None
        if agent is not None:
            done_gate.require_local(store, args.slug, agent.task)
            _require_target(store, args, agent)
        if args.action == "queue":
            result = merge_queue.operate(
                args.action,
                args.url,
                admit=lambda head: review.queue_admit(
                    store, args, agent, _me(store, args), head, _queue_intent, stalled
                ),
                journal=queue_record.Journal(args.url, context=stalled or (agent and {"task": agent.task})),
                attempts=merge_queue.ATTEMPTS,
                priority=lambda: queue_priority.automatic(store, args.slug, args.url, LedgerClient()),
            )
        else:
            result = merge_queue.operate(args.action, args.url, journal=queue_record.Journal(args.url))
    print(json.dumps(result))


def cmd_done(store: RedisStore, args: argparse.Namespace) -> None:
    agent = _worker(store, args)
    if agent.lane == dispatch_seat.LANE:
        return _dispatcher_done(store, args.slug, agent)
    done_gate.require_local(store, args.slug, agent.task)
    ledger = LedgerClient()
    row = ledger.task(args.slug, agent.task) or {}
    proof = {key: getattr(args, f"proof_{key}") for key in ledger_kinds.PROOF_KEYS if getattr(args, f"proof_{key}")}
    if ledger_kinds.kind(row) == "workflow":
        if (found := _workflow_proof(store, args.slug, agent, ledger, row)) is None:
            return
        proof.update(found)
    proven = {**row, "proof": {**(row.get("proof") or {}), **proof}}
    review.require_done(store, args.slug, ledger_kinds.kind(row), agent.lane, agent.task)
    missing = ledger_kinds.unmet(proven)
    if missing:
        raise SwarmError(_unproven(ledger_kinds.kind(row), missing))
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
    _feature_gate(store, args.slug, agent, proven, pull)
    closing = enhance_review.close(args.slug, row, store.config(args.slug).autonomy)
    fields = {**done_gate.DONE, **({"pr_url": args.pr} if args.pr else {}), **({"proof": proof} if proof else {})}
    closed = ledger.update_task(args.slug, agent.task, fields, by=agent.name)
    enhance_review.settle(ledger, InboxStore(store.redis), args.slug, agent.name, closing)
    _close_members(ledger, args.slug, agent, row, {**fields, "pr_url": args.pr or row.get("pr_url")})
    waits.settle_notices(InboxStore(store.redis), agent, "done")
    _retire(store, args.slug, agent, "finished its task and exited")
    doc_report.report(
        InboxStore(store.redis), ledger, args.slug, closed, now_ms(), store.config(args.slug).watchdog_address
    )
    fallback = {"pull_request": pull.fallback} if pull is not None and pull.fallback else {}
    print(
        json.dumps({"task": agent.task, "state": "done", **fallback, "next": "stop now; the swarm closes this session"})
    )


def _unproven(kind, missing):
    if kind == "research":
        return (
            "a research task is done only with its proof: "
            "--finding must be a single link to the artifact; put prose in a task comment"
        )
    if kind == "workflow":
        return (
            f"a workflow task is done only when {workflow_task.STATE} in its workspace says passed "
            f"and the judge verdict record {workflow_task.VERDICT} says pass"
        )
    flags = ", ".join("--" + key.replace("_", "-").replace(" or ", " or --") for key in missing)
    return f"a {kind} task is done only with its proof: give {flags}"


def _workflow_proof(store, slug, agent, ledger, row):
    workspace = row.get("workspace")
    notes = workflow_task.exhausted_notes(workspace)
    if notes is None:
        return workflow_task.proof(workspace)
    block_agent(store, slug, agent, notes, ledger)
    print(json.dumps({"task": agent.task, "state": "blocked", "next": "stop now; the swarm closes this session"}))
    return None


def _pull_reader(url, pull):
    return lambda target: pull if target == url else ledger_events.view(target)


def _feature_gate(
    store: RedisStore, slug: str, agent: AgentRecord, task: dict[str, Any], pull: ledger_events.PullRequest | None
) -> None:
    """Refuse swarm done when the feature gate enforces and the task names no mapped, tested feature."""
    if modes.configured(feature_gate.GATE, store.config(slug).gates) == "off":
        return
    head = pull.head if pull is not None else ""
    wanted = task["proof"].get("feature", "")
    refused = feature_gate.refusal(
        task, wanted, lambda: [feature_gate.read_feature(ref, wanted) for ref in feature_gate.refs(head)]
    )
    who = Who(name=agent.name, swarm=slug, task=agent.task)
    if refused and feature_gate.GATE.allows(store, who, "done", refused, now_ms()):
        raise SwarmError(refused)


def _dispatcher_done(store, slug, agent):
    refused = dispatch_seat.refusal(slug, store.config(slug), store, LedgerClient().state(slug), now_ms())
    if refused:
        raise SwarmError(refused)
    _retire(store, slug, agent, "settled its triggers and exited")
    print(json.dumps({"seat": agent.name, "state": "finished", "next": "stop now; the swarm closes this session"}))


def _close_members(ledger, slug, agent, lead, fields):
    for member in lead.get("group_members") or []:
        task = ledger.task(slug, member)
        if task is not None and task["state"] != "done":
            ledger.update_task(slug, member, fields, by=agent.name)


def cmd_block(store, args):
    agent = _worker(store, args)
    ledger = LedgerClient()
    row = ledger.task(args.slug, agent.task)
    if row is None:
        raise SwarmError(f"ledger {args.slug} has no task {agent.task}")
    for dependency in prerequisite_wait.dependencies(store, args.slug, agent, row, args.note):
        if (ledger.task(args.slug, dependency) or {}).get("state") != "done":
            held = waits.on("task", dependency)
            at = now_ms()
            until = at + waits.CHECKED_MINUTES * 60_000
            idle.declare_wait(store.redis, args.slug, agent.name, until, args.note, at, on=held)
            waits.settle_notices(InboxStore(store.redis), agent, "a new wait")
            print(json.dumps({"task": agent.task, "state": "claimed", "waits_on": held}))
            return
    from scripts.swarm import publish

    refused = "" if args.dev_red else _block_refusal(store, args.slug, row)
    if refused:
        raise SwarmError(refused)
    pushed = publish.pushed_branches(_local_repo(store, args.slug), row, agent)
    red_run = _dev_red_run(store, args.slug) if args.dev_red else None
    if pushed:
        ledger.update_task(args.slug, agent.task, pushed, by=agent.name)
    block_agent(store, args.slug, agent, args.note, ledger, red_run)
    cause = {"dev_red_run": red_run} if red_run is not None else {}
    print(
        json.dumps({"task": agent.task, "state": "blocked", **cause, "next": "stop now; the swarm closes this session"})
    )


def _block_refusal(store, slug, task):
    from scripts.swarm import resolve

    return resolve.block_refusal(store.config(slug).autonomy, slug, task)


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
    prerequisite_wait.hold(store, slug, agent, note)
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
    if args.final and (not args.on or args.on[0] != "merge"):
        raise SwarmError("--final requires --on merge for the task final deliverable")
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
        get = InboxStore(store.redis).locate
        if problem := waits.target_problem(held["kind"], held["target"], agent.task, rows, get):
            raise SwarmError(problem)
        prerequisite_wait.admit(store, args.slug, agent, held, LedgerClient(), now_ms(), args.final)
        if held["kind"] == "checks" and waits.PULL_URL.fullmatch(held["target"]):
            head = ledger_events.head(held["target"])
            if head == "":
                raise SwarmError("cannot read the pull request head; retry the checks wait")
            if head is None:
                waits.run_wait.admitted_pending(held["target"])
            held["head"] = head
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


def cmd_plan(store: RedisStore, args: argparse.Namespace) -> None:
    agent, autonomy = _me(store, args), store.config(args.slug).autonomy
    decision = plan_review.Decision(args.action, args.note, args.override, args.task)
    result = plan_review.decide(LedgerClient(), args.slug, agent, autonomy, args.phase, decision)
    print(json.dumps(result))


def cmd_handoff(store: RedisStore, args: argparse.Namespace) -> None:
    agent: AgentRecord = _me(store, args)
    text = _read(args.doc, "handoff document")
    ledger = LedgerClient()
    found = handoff_contracts.problems(text, Resolver(args.slug, store.redis, ledger.item), store, args.slug, agent)
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


def _hand_off(
    store: RedisStore, slug: str, agent: AgentRecord, text: str, reason: str, ledger: Any
) -> tuple[dict, dict]:
    recap = "\n\n".join(
        f"## {heading}\n{handoff_check.section(text, heading)}" for heading in ("Done", "Stopped at", "Next")
    )
    waits.settle_notices(InboxStore(store.redis), agent, "a handoff")
    rows = _own_rows(ledger, slug, agent.task)
    envelope = handoff_envelope.build(store, slug, agent, reason, rows, now_ms())
    envelope = handoff_envelope.with_orders(envelope, store, ledger, agent)
    store.memory.add_recap(_seat(agent), agent.name, agent.task, recap, now_ms())
    store.put_handoff(slug, agent.task, text, seat=agent.seat, envelope=envelope)
    if hive_release.release(ledger, slug, (rows or [{}])[0], agent.name):
        store.release(slug, agent.task, agent.name)
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
    row = ledger.task(args.slug, agent.task) or {}
    found = ready.check(
        row,
        agent,
        store.config(args.slug).autonomy,
        ledger_events.view,
        now_ms(),
        ready.continuation(store, args.slug, agent.task),
    )
    problems = handoff_check.problems(text, Resolver(args.slug, store.redis, ledger.item))
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


def cmd_confirm_handoff(store: RedisStore, args: argparse.Namespace) -> None:
    agent = _me(store, args)
    live = HerdrRuntime().live_names()
    handoff_commands.confirm(store, args.slug, args.transfer, agent, args.next, live, now_ms())


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


def _own_rows(ledger, slug, task_id):
    try:
        row = ledger.task(slug, task_id)
    except SwarmError:
        return None
    return [row] if row else []


def cmd_learned(store: Any, args: argparse.Namespace) -> None:
    if not args.text:
        seat = "" if args.all else _seat(_me(store, args))
        _list_learned(store, args.slug, seat, all_seats=args.all)
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


def _list_learned(store: Any, slug: str, seat: str, *, all_seats: bool) -> None:
    store.config(slug)
    for note in store.memory.scoped(slug, seat, all_seats=all_seats):
        print(f"{note['seat']}\t{note['number']}\t{note['maturity']}\t{note['text']}")


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


def _read(path: str, what: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the {what} {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise SwarmError(f"cannot read the {what} {path}: it is not UTF-8 text") from exc


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
        raise SwarmError(front_door.say_refusal(args.slug, agent.name))
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
    if argv[:1] == ["brain-place"]:
        from scripts.swarm import brain_place

        return brain_place.main(argv[1:])
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
        handler = vars(args).get("handler") or globals()[f"cmd_{args.command.replace('-', '_')}"]
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
        before = control_notifications.master(store, args.slug) if action in control_notifications.CONTROLS else None
        if action in clearance.COMMANDS:
            run_control(store, args, who, handler)
        else:
            handler(store, args)
        panic.settle()
        if action in control_notifications.CONTROLS:
            control_notifications.notify(store, args, LedgerClient(), before, action)
    except (SwarmError, InboxError, RedisError) as exc:
        panic.settle()
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
