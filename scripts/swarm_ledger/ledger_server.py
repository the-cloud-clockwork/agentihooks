#!/usr/bin/env python3
"""Local ledger server: serves the ledger pages and reads/writes the ledger records in ~/development-ledger.

Usage:
  ledger_server.py --ensure   start it detached if it is not answering, print the base URL
  ledger_server.py --serve    run in the foreground
  ledger_server.py --stop     stop the detached server
  ledger_server.py --reload   hand the running server's socket to a fresh process on the current code

Env: LEDGER_DIR (default ~/development-ledger), LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765),
SWARM_PUBLIC_URL and SWARM_ALLOWED_HOSTS (comma list) beside loopback, LEDGER_TLS_CERT and LEDGER_TLS_KEY to serve
https (clients trust LEDGER_CA_PEM), SWARM_RELOAD=1 for code reload
(--ensure sets it unless given), LEDGER_IMPECCABLE_LIVE=1 for the Impeccable live origin on a scratch server.
Idempotent: --ensure on a running server only prints the URL.
"""

import argparse
import errno
import functools
import html
import http.server
import importlib
import itertools
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
import ledger_alerts  # noqa: E402
import ledger_artifacts  # noqa: E402
import ledger_authority as authority  # noqa: E402
import ledger_bin  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate  # noqa: E402
import ledger_layout  # noqa: E402
import ledger_link  # noqa: E402
import ledger_media  # noqa: E402

from scripts.gates import talk  # noqa: E402
from scripts.swarm_ledger import (  # noqa: E402
    composition,
    ledger_blockers,
    ledger_closed,
    ledger_task_duplicates,
    page_policy,
    ports,
    request_metrics,
    server_code,
    server_compose,
    server_handoff,
    server_lifetime,
    storage,
)
from scripts.swarm_ledger.events import Hub  # noqa: E402
from scripts.swarm_ledger.events.publishing import publishing  # noqa: E402
from scripts.swarm_ledger.repository import (
    hierarchy,  # noqa: E402
    legacy,  # noqa: E402
)
from scripts.swarm_ledger.server_code import CODE_DIRS, code_digest, code_stamp  # noqa: E402

HOST, PORT = ledger_link.address()
BASE = f"{ledger_link.scheme()}://{HOST}:{PORT}"
PIDFILE = core.LEDGER_DIR / ".server.pid"
LOGFILE = core.LEDGER_DIR / ".server.log"
SERVER_WAIT = 5.0
COMPOSE_WAIT = 180.0
POLL_S = 0.05
DRAIN_S = 5.0
LEASE_WAIT_S = 20.0
RELOAD_WAIT_S = 100.0
WARM_PASSES = 5
CARRY = ".server.carry.json"
WATCHED = "LEDGER_WATCHED"
MODULES = "LEDGER_MODULES"
RELOAD = threading.Event()
HANDING = threading.Lock()
HANDED = threading.Event()
NEVER = threading.Event()
WATCH_PASS = threading.Lock()
HANDOFF = {}
THREADS_EXHAUSTED = 75
TLS_HANDSHAKE_S = 10
PROBE_FILE = ".healthz"
FILE_ORIGIN = "null"
MAX_BODY = 1 << 20
ALLOWED_HOSTS = ledger_link.allowed_hosts()
ALLOWED_ORIGINS = ledger_link.allowed_origins()
CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
LOGO = ROOT / "media" / "agentihooks-logo.png"


HUB = Hub()
BIN_SWEEP_EVERY = 15
FOLLOW_S = 0.25
SCHEDULER_UNAVAILABLE = threading.Event()
LOADED_STAMP = code_stamp()
LOADED_DIGEST = code_digest()


@functools.cache
def served_page():
    """The assets this process serves and their version, read once so a version URL never serves other bytes."""
    assets = {name: path.read_bytes() for name, path in core.static_assets().items()}
    return core.page_version(assets), assets


def served_version():
    return served_page()[0]


NO_DOCTOR = ("", False)


def page_view(state, fix_phase, watched):
    meta = {key: item for key, item in state["_meta"].items() if key not in ("seeds", "api_operations")}
    swarm = ports.swarm()
    marks = swarm.freeze.marks(state, hierarchy.project(state)[0], fix_phase, watched)
    status = swarm.phase_state.status({"tasks": [], **state})
    rows, closed = ledger_closed.trimmed(state)
    if closed:
        meta["closed"] = closed
    return {**state, **rows, "marks": marks, "phase_status": status, "_meta": {**meta, "limits": PAGE_LIMITS}}


def doctor_holds(slug):
    from redis import RedisError

    SwarmError = ports.swarm().store.SwarmError
    try:
        store = swarm_store()
        fix = ports.swarm().freeze.fix_phase(store.config(slug))
        peer = fix and store.peer(slug)
        records = repository.current(peer).state.get("freezes") or [] if peer and repository.exists(peer) else []
    except (SwarmError, RedisError, ValueError, OSError):
        return NO_DOCTOR
    return fix, any(r.get("verb") == "focus" for r in records)


def ledger_view(state, slug=None):
    view = page_view(state, *(doctor_holds(slug) if slug else NO_DOCTOR))
    view["_meta"].update(page_version=served_version(), crew=ledger_gate.crew(state["_meta"]))
    return view


def publish_ledger(slug, state):
    if HUB.has(slug):
        HUB.publish(slug, "ledger", ledger_view(state, slug))


def publish_snapshot(slug: str, state: dict) -> None:
    if HUB.has(slug):
        HUB.publish_snapshot(slug, "ledger", ledger_view(state, slug))


stored = storage.default()
repository = publishing(stored, publish_snapshot)


def all_summaries():
    return repository.list_summaries()


def ledger_summaries():
    binned = ledger_bin.entries()
    return [s for s in all_summaries() if s["slug"] not in binned]


def bin_summaries(now=None):
    now = core.now_ms() if now is None else now
    binned = ledger_bin.entries()
    found = [
        {**s, "deleted_at": binned[s["slug"]], "days_left": ledger_bin.days_left(binned[s["slug"]], now)}
        for s in all_summaries()
        if s["slug"] in binned
    ]
    return sorted(found, key=lambda s: s["deleted_at"], reverse=True)


PAGE_POLICY = page_policy.policy(os.environ, core.LEDGER_DIR, PORT)
STATIC_RE = re.compile(r"/static/([0-9a-f]{12})/((?:[a-z]+/)?[a-z_]+\.(js|css))")
STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
ASSET_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}
PAGE_HEADERS = {"Content-Security-Policy": PAGE_POLICY, "X-Content-Type-Options": "nosniff"}


def home_summaries():
    return [{**s, "swarm": swarm_state(s["slug"])} for s in ledger_summaries()]


def index_page():
    """The HOME and BIN shell; its script builds the view and loads its rows from the v1 ledger and bin collections."""
    return core.HOME.read_bytes().decode().replace("__HOME_PAGE__", served_version())


def page_for(slug):
    """The ledger shell: the page's metadata and asset links, read without writing; the records load over the API."""
    title = repository.read(slug, "title").get("title") or slug
    values = {
        "TOKEN": html.escape(repository.token(slug) or ""),
        "PAGE": served_version(),
        "SLUG": html.escape(slug),
        "PORT": str(PORT),
        "TITLE": html.escape(title),
    }
    return re.sub(
        r"__LEDGER_(TOKEN|PAGE|SLUG|PORT|TITLE)__", lambda m: values[m.group(1)], core.SHELL.read_text(encoding="utf-8")
    )


@functools.cache
def swarm_store():
    connect = ports.swarm().store.connect

    return connect()


def swarm_status(slug, state=None):
    commands = ports.swarm().commands
    SwarmError = ports.swarm().store.SwarmError

    try:
        return commands.view(swarm_store(), slug)
    except SwarmError:
        return None
    except Exception as exc:
        sys.stderr.write(f"swarm status {slug}: {exc}\n")
        return None


STREAM_UNREAD = ("history", "transfers", "launch_checks")


def stream_swarm(slug, ledger):
    """Swarm status for the page stream, without the records the page never reads."""
    status = swarm_status(slug, ledger)
    return status and {key: item for key, item in status.items() if key not in STREAM_UNREAD}


def swarm_models():
    model_catalog = ports.swarm().model_catalog
    SwarmError = ports.swarm().store.SwarmError

    try:
        redis = swarm_store().redis
    except SwarmError:
        redis = None
    return model_catalog.catalog(dict(os.environ), redis, time.time())


def swarm_state(slug):
    SwarmError = ports.swarm().store.SwarmError

    try:
        return swarm_store().config(slug).state
    except SwarmError:
        return None
    except Exception as exc:  # HOME still renders; the log keeps why this read failed
        sys.stderr.write(f"swarm state {slug}: {exc}\n")
        return None


def bin_closed_without_swarm(now=None):
    SwarmError = ports.swarm().store.SwarmError

    try:
        store = swarm_store()
        for s in ledger_summaries():
            if not s["closed_at"]:
                continue
            try:
                store.config(s["slug"])
            except SwarmError:
                ledger_bin.bin_closed(s["slug"], s["closed_at"], now)
    except Exception as exc:  # the tick bins these once the swarm store answers again
        sys.stderr.write(f"bin closed ledgers: {exc}\n")


CONTROLS = {
    "start": ["start"],
    "pause": ["pause"],
    "stop": ["stop"],
    "stop_now": ["stop", "--now"],
    "close": ["close"],
    "reopen": ["reopen"],
}
DOCTOR = {"doctor_start": ["start"], "doctor_stop": ["stop"]}
DOCTOR_PHRASE = "rig doctor stop"
MAX_CAP = 50
MIN_COMPACT, MAX_COMPACT = 100, 1000
QUOTA_PROBE_TIMEOUT_S = 120
AUTONOMY = ("manual", "assist", "delegate", "full")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SCALING = ("auto", "manual")
MAX_LOAD = 10.0
MASTER_AGENTS = ("claude", "codex")
MAX_NOTE = 500
FINDING_RE = re.compile(r"^[a-z][a-z-]*/[\w.-]{1,64}$")
CAPS = {
    "max_eng": ("max-eng-agents", 0, MAX_CAP),
    "max_ci": ("max-ci-agents", 0, MAX_CAP),
    "max_plan": ("max-plan-agents", 0, MAX_CAP),
    "masters_step": ("masters-step", 0, MAX_CAP),
    "compact_limit": ("compact-limit", MIN_COMPACT, MAX_COMPACT),
}
PAGE_LIMITS = {
    "capacity": {key: [low, high] for key, (_, low, high) in CAPS.items()},
    "efforts": list(EFFORTS),
    "sync_cooldown_ms": core.SYNC_COOLDOWN_MS,
    "bin_days": ledger_artifacts.KEEP_DAYS,
}


def model_pairs(body):
    default_models = ports.swarm().default_models

    changes = {key: body[key] for key in default_models.FIELDS.values() if key in body}
    if not changes:
        return []
    if refused := default_models.refusal(changes, swarm_models()):
        raise ValueError(refused)
    return [f"{key}={changes[field]}" for key, field in default_models.KEYS.items() if field in changes]


def control_argv(body):
    action = body.get("action") if isinstance(body, dict) else None
    if action in CONTROLS:
        return CONTROLS[action]
    if action == "terminate":
        name = body.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9@._-]{1,200}", name) or name.startswith("-"):
            raise ValueError("terminate needs an exact agent name")
        return ["terminate", name]
    builders = {
        "verdict": verdict_argv,
        "restore-decision": restore_decision_argv,
        "lift": lift_argv,
        "master_move": master_move_argv,
    }
    if action in builders:
        return builders[action](body)
    if action != "set":
        raise ValueError("action must be start, pause, stop, stop_now, close, reopen, set or verdict")
    pairs = []
    for key, (flag, low, limit) in CAPS.items():
        value = body.get(key)
        if value is None:
            continue
        if type(value) is not int or not low <= value <= limit:
            raise ValueError(f"{key} must be a whole number from {low} to {limit}")
        pairs.append(f"{flag}={value}")
    for key, flag in (("effort_min", "effort-min"), ("effort_max", "effort-max")):
        if key in body:
            if body[key] not in EFFORTS:
                raise ValueError(f"{key} must be one of {', '.join(EFFORTS)}")
            pairs.append(f"{flag}={body[key]}")
    if "autonomy" in body:
        if body["autonomy"] not in AUTONOMY:
            raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY)}")
        pairs.append(f"autonomy={body['autonomy']}")
    pairs += scaling_pairs(body)
    pairs += model_pairs(body)
    if "gates" in body:
        pairs += gate_pairs(body["gates"])
    if "master_agent" in body:
        if body["master_agent"] not in MASTER_AGENTS:
            raise ValueError(f"master_agent must be one of {', '.join(MASTER_AGENTS)}")
        pairs.append(f"master-agent={body['master_agent']}")
    if "overlays" in body:
        pairs += overlay_pairs(body["overlays"])
    if not pairs:
        raise ValueError(
            "set needs max_eng, max_ci, max_plan, masters_step, compact_limit, effort_min, effort_max, autonomy, "
            "scaling, load_high, load_low, memory_per_agent_mb, claude_model, codex_model, master_agent, overlays or gates"
        )
    return ["set", *pairs]


def effort_refusal(slug, body):
    if body.get("action") != "set" or not {"effort_min", "effort_max"} & set(body):
        return None
    from redis import RedisError

    effort_range, SwarmError = ports.swarm().effort_range, ports.swarm().store.SwarmError
    try:
        config = swarm_store().config(slug)
    except (SwarmError, RedisError):
        return None
    bounds = (body.get("effort_min", config.effort_min), body.get("effort_max", config.effort_max))
    return effort_range.refusal(bounds, config.lanes)


def scaling_pairs(body):
    pairs = []
    if "scaling" in body:
        if body["scaling"] not in SCALING:
            raise ValueError(f"scaling must be one of {', '.join(SCALING)}")
        pairs.append(f"scaling={body['scaling']}")
    for key, flag in (("load_high", "load-high"), ("load_low", "load-low")):
        if key in body:
            value = body[key]
            if type(value) not in (int, float) or not 0 < value <= MAX_LOAD:
                raise ValueError(f"{key} must be a number above 0 and at most {MAX_LOAD:g}")
            pairs.append(f"{flag}={value}")
    if body.get("load_low", 0) > body.get("load_high", MAX_LOAD):
        raise ValueError("load_low must be at most load_high")
    if "memory_per_agent_mb" in body:
        value = body["memory_per_agent_mb"]
        if type(value) is not int or value <= 0:
            raise ValueError("memory_per_agent_mb must be a whole number of MB above 0")
        pairs.append(f"memory-per-agent={value}")
    return pairs


def overlay_pairs(overlays):
    OVERLAY_CAP = ports.hooks().profile_chain.OVERLAY_CAP
    KEY, ROLES = ports.swarm().overlays.KEY, ports.swarm().overlays.ROLES

    name = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
    if (
        not isinstance(overlays, dict)
        or not overlays
        or not all(
            role in ROLES
            and isinstance(names, list)
            and len(set(names)) == len(names)
            and all(isinstance(n, str) and name.fullmatch(n) for n in names)
            for role, names in overlays.items()
        )
    ):
        raise ValueError(f"overlays maps a base role of {', '.join(ROLES)} to a list of distinct overlay names")
    for role, names in overlays.items():
        if len(names) > OVERLAY_CAP:
            raise ValueError(f"a role wears at most {OVERLAY_CAP} overlays; {role} was given {len(names)}")
    return [f"{KEY}{role}={','.join(overlays[role])}" for role in ROLES if role in overlays]


def gate_pairs(gates):
    from scripts.gates import catalog, modes

    names = catalog.defaults()
    if (
        not isinstance(gates, dict)
        or not gates
        or not all(
            n in names and isinstance(m, str) and modes.normalize(m) in modes.supported(n) for n, m in gates.items()
        )
    ):
        raise ValueError(
            f"gates maps a gate of {', '.join(names)} to {', '.join(modes.label(mode) for mode in modes.MODES)}"
        )
    return [f"{name}-gate={mode}" for name, mode in gates.items()]


def restore_decision_argv(body):
    agent, choice = body.get("agent"), body.get("choice")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9@_.-]{0,127}", agent):
        raise ValueError("A restore decision needs an agent name")
    if choice not in {"resume", "native", "context", "fresh"}:
        raise ValueError("Choose native, context, fresh or resume")
    return ["restore-decision", agent, choice]


def lift_argv(body):
    agent, gate = body.get("agent"), body.get("gate")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9@_.-]{0,127}", agent):
        raise ValueError("A lift needs an agent name")
    if not isinstance(gate, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", gate):
        raise ValueError("A lift needs the gate's name")
    return ["lift", agent, gate]


def master_move_argv(body):
    HIVE_ID = ports.hive().registry.HIVE_ID

    hive = body.get("hive")
    if not isinstance(hive, str) or not HIVE_ID.fullmatch(hive):
        raise ValueError("A master move needs a hive id")
    return ["master", "move", "--to", hive]


def verdict_argv(body):
    VERDICTS = ports.swarm().verdicts.VERDICTS

    finding, value, note = body.get("id"), body.get("verdict"), body.get("note", "")
    if not (isinstance(finding, str) and FINDING_RE.match(finding)):
        raise ValueError("id must name a finding, kind/subject")
    if value not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ValueError(f"note must be text of at most {MAX_NOTE} characters")
    return ["--as", "operator", "verdict", finding, value, f"--note={note}"]


def bin_request(body):
    if not isinstance(body, dict) or body.get("action") not in ("delete", "restore"):
        raise ValueError("action must be delete or restore")
    if not isinstance(body.get("slug"), str):
        raise ValueError("slug must be a string")
    return body["action"], body["slug"]


def terminate_control(slug, name):
    commands = ports.swarm().commands

    store = swarm_store()
    if not any(agent.name == name for agent in store.agents(slug)):
        return None, "agent is not in this swarm"
    commands.submit(store, slug, "swarm", ["terminate", name])
    return swarm_status(slug), ""


def swarm_control(slug, argv, command="swarm"):
    commands = ports.swarm().commands
    SwarmError = ports.swarm().store.SwarmError

    try:
        if command == "swarm" and argv[0] == "terminate":
            return terminate_control(slug, argv[1])
        commands.submit(swarm_store(), slug, command, argv)
        return swarm_status(slug), ""
    except SwarmError as exc:
        return None, str(exc)


def lifecycle_control(slug, body, operator):
    lifecycle = ports.swarm().lifecycle
    SwarmError = ports.swarm().store.SwarmError

    try:
        stamps = repository.read(slug, "_meta.stamps").get("_meta", {}).get("stamps", {})
    except KeyError:
        return None, "no such ledger"
    try:
        lifecycle.request(swarm_store(), slug, body, operator=operator, task_revisions=lifecycle.revisions(stamps))
    except SwarmError as exc:
        return None, str(exc)
    return swarm_status(slug), ""


def refresh_quota(slug: str) -> tuple[dict | None, str]:
    quota_refresh = ports.swarm().quota_refresh
    SwarmError = ports.swarm().store.SwarmError

    try:
        quota_refresh.request(swarm_store(), slug)
    except SwarmError as exc:
        return None, str(exc)
    return swarm_status(slug), ""


def relay_to_inbox(slug, state):
    """Operator writes since the oldest revision not yet relayed become inbox items for the swarm on this ledger."""
    meta = state["_meta"]
    since = unrelayed_since(RELAY, slug, meta["rev"])
    if since <= meta.get("events_trimmed", -1):
        since = record_trimmed(slug, since, meta["events_trimmed"])
    events = [
        e for e in meta.get("events", []) if since <= e.get("rev", -1) <= meta["rev"] and e.get("by") == "operator"
    ]
    if not events:
        relayed(RELAY, slug, since, meta["rev"])
        return []
    try:
        import watch_ledger

        from scripts.inbox.store import connect

        operator_mail = ports.swarm().operator_mail
        RedisStore = ports.swarm().store.RedisStore
        from scripts.swarm_ledger import chat_receipts

        masters = ports.swarm().masters

        inbox = connect()
        store = RedisStore(inbox.redis)
        sent = operator_mail.relay(inbox, store, slug, state, events, watch_ledger.line)
        addresses = [masters.lead_of(slug, []), *masters.held(store.redis, slug, store.agents(slug))]
        chat_receipts.post(repository, inbox, slug, events, addresses)
    except Exception as exc:  # the ledger write stands whatever the inbox does
        sys.stderr.write(f"inbox relay for {slug}: {exc}\n")
        unrelayed_on(RELAY, slug, meta["rev"])
        return []
    relayed(RELAY, slug, since, meta["rev"])
    return sent


TRIMMED = (
    "Operator writes from revision {since} through {trimmed} left the ledger history before the inbox could take "
    "them during an outage, so no agent received them. Write them again if they still matter."
)


def record_trimmed(slug, since, trimmed):
    """Tell the operator which unrelayed writes the event window dropped; the relay resumes after them."""
    notice = {"op": "notice", "id": f"trimmed-{since}-{trimmed}", "by": "ledger", "text": TRIMMED.format(**locals())}
    try:
        _, rejected = repository.apply_ops(slug, ops=[notice])
    except Exception as exc:  # the ledger write stands; the held revisions wait for the next relay
        sys.stderr.write(f"trimmed notice for {slug}: {exc}\n")
        return since
    if rejected:
        return since
    relayed(RELAY, slug, since, trimmed)
    return trimmed + 1


def deliver_alerts(slug, state):
    meta = state["_meta"]
    since = unrelayed_since(ALERTS, slug, meta["rev"])
    fresh = [
        a
        for a in state.get("alerts", [])
        if since <= a.get("rev", -1) <= meta["rev"] and a["state"] == ledger_alerts.OPEN
    ]
    if not fresh:
        relayed(ALERTS, slug, since, meta["rev"])
        return []
    try:
        from scripts.inbox.store import connect

        operator_mail = ports.swarm().operator_mail
        RedisStore = ports.swarm().store.RedisStore

        inbox = connect()
        agents = RedisStore(inbox.redis).agents(slug)
        live = [a for a in agents if a.state != "finished"]
        master = operator_mail.master_address(inbox.redis, slug, agents)
        addresses = {a.name: a.seat or a.name for a in live}
        alerts = [{**a, "target": addresses.get(a["target"], "master")} for a in fresh]
        tier = ledger_blockers.autonomy(slug)
        sent = ledger_alerts.deliver(inbox, slug, alerts, meta["rev"], master, since=since, tier=tier)
    except Exception as exc:  # the ledger write stands whatever the inbox does
        sys.stderr.write(f"alert delivery for {slug}: {exc}\n")
        unrelayed_on(ALERTS, slug, meta["rev"])
        return []
    relayed(ALERTS, slug, since, meta["rev"])
    return sent


RELAY, ALERTS = "relay", "alerts"
UNRELAYED = "ledger-unrelayed.json"
REPLAY_EVERY = 5


def unrelayed() -> dict:
    try:
        return stored.held_revisions()
    except (storage.Error, OSError):
        return {}


def unrelayed_since(kind, slug, rev) -> int:
    return min([rev, *unrelayed().get(kind, {}).get(slug, [])])


def unrelayed_on(kind, slug, rev):
    """Keep the revision for replay whatever stopped the inbox from taking it."""
    stored.hold_revision(kind, slug, rev)


def relayed(kind, slug, since, rev):
    """Forget the revisions a relay from since through rev covered; one recorded meanwhile outside it stays."""
    if unrelayed().get(kind, {}).get(slug):
        stored.release_revisions(kind, slug, since, rev)


def adopt_unrelayed() -> None:
    config = ports.hooks().config

    path = Path(config.AGENTIHOOKS_HOME) / UNRELAYED
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    for kind, slugs in found.items() if isinstance(found, dict) else ():
        for slug, revisions in slugs.items() if isinstance(slugs, dict) else ():
            for rev in revisions if isinstance(revisions, list) else ():
                if isinstance(rev, int):
                    unrelayed_on(kind, slug, rev)
    path.unlink()


def replay_unrelayed() -> None:
    """Relay again every revision the inbox could not take, from the stored ledger, once Redis answers."""
    for kind, slugs in unrelayed().items():
        for slug in list(slugs):
            try:
                state = repository.get_document(slug)
            except KeyError:
                stored.drop_revisions(kind, slug)
                continue
            REPLAYS[kind](slug, state)


REPLAYS = {RELAY: relay_to_inbox, ALERTS: deliver_alerts}


def expire_alerts() -> None:
    at = core.now_ms()
    for summary in ledger_summaries():
        slug = summary["slug"]
        state = repository.read(slug, "alerts")
        if any(ledger_alerts.expired(alert, at) for alert in state.get("alerts", [])):
            repository.apply_ops(slug)


def doctor_phrase(slug, state):
    meta = state["_meta"]
    said = [
        e
        for e in meta.get("events", [])
        if e.get("rev") == meta["rev"] and e.get("by") == "operator" and e.get("target") == "chat"
    ]
    if any(e.get("text", "").strip().lower() == DOCTOR_PHRASE for e in said):
        swarm_control(slug, ["stop"], "doctor")


def stream_resources(slug):
    ledger = ledger_view(repository.get_document(slug), slug)
    return {"ledger": ledger, "swarm": stream_swarm(slug, ledger)}


def workspace_tails(slug, task_id):
    """The latest work folder lines the hive tick published for one task, read when its proof fold opens."""
    commands = ports.swarm().commands
    SwarmError = ports.swarm().store.SwarmError

    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError(f"no work folder for task {task_id!r}")
    try:
        return commands.workspaces(swarm_store(), slug).get(task_id, {})
    except SwarmError:
        return {}


def write_gate(slug):
    """Every op write: masters fenced to the items they own, page chat to the front door, asks of the operator behind
    the blocker protocol, inside the talk budget."""
    swarm = ports.swarm()
    protocol = ledger_blockers.Gate(slug, talk.Budget(slug))
    return swarm.master_fence.Fence(slug, swarm.front_door.Door(slug, protocol))


def api_services():
    from scripts.swarm_ledger import services
    from scripts.swarm_ledger.api.services import HttpPolicy, LedgerService, MediaService, Services, SwarmService

    ledger: LedgerService = services.Ledger(
        core=core,
        authority=authority,
        hub=HUB,
        layout_rows=ledger_layout.ROWS,
        layout_lists=ledger_layout.LISTS,
        home_summaries=home_summaries,
        bin_summaries=bin_summaries,
        bin_entries=ledger_bin.entries,
        bin_ledger=ledger_bin.delete,
        workspace_tails=workspace_tails,
        stream_resources=stream_resources,
        layout=ledger_layout.read,
        write_layout=ledger_layout.write,
        write_gate=write_gate,
        relay_to_inbox=relay_to_inbox,
        deliver_alerts=deliver_alerts,
    )
    swarm: SwarmService = services.Swarm(
        doctor=DOCTOR,
        store=swarm_store,
        status=swarm_status,
        models=swarm_models,
        control=swarm_control,
        lifecycle=lifecycle_control,
        refresh_quota=refresh_quota,
        control_argv=control_argv,
        doctor_phrase=doctor_phrase,
    )
    media: MediaService = services.Media(
        media_store=ledger_media,
        artifact_store=ledger_artifacts,
        resolve_media=ledger_media.resolve,
        resolve_artifacts=ledger_artifacts.resolve,
    )
    return Services(
        repository=repository,
        ledger=ledger,
        swarm=swarm,
        media=media,
        policy=HttpPolicy(allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS, max_body=MAX_BODY),
    )


def sample_streams():
    """Publish swarm status for every ledger a stream is open on."""
    HUB.evict()
    for slug in HUB.watched():
        ledger = HUB.resource(slug, "ledger")
        if ledger is None:
            continue
        try:
            HUB.publish(slug, "swarm", stream_swarm(slug, ledger))
        except Exception as exc:  # the seed watcher that calls this must outlive any one ledger's readers
            sys.stderr.write(f"stream sample {slug}: {exc}\n")


def request_caller(headers) -> str:
    if headers is None:
        return "-"
    if headers.get("X-Ledger-Agent"):
        return headers["X-Ledger-Agent"]
    if headers.get("X-Ledger-Tick"):
        return "tick"
    return "controller" if headers.get("X-Controller-Credential") else "-"


class Handshake(Exception):
    pass


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        self.active = 0
        self.idle = threading.Condition()
        super().__init__(*args, **kwargs)

    def handle_error(self, *args):
        if not isinstance(sys.exc_info()[1], Handshake):
            super().handle_error(*args)

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        with self.idle:
            self.active += 1
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            os._exit(THREADS_EXHAUSTED)

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.idle:
                self.active -= 1
                self.idle.notify_all()

    def drained(self, timeout: float) -> bool:
        with self.idle:
            return self.idle.wait_for(lambda: not self.active, timeout)


class Handler(BaseHTTPRequestHandler):
    tls: ssl.SSLContext | None = ledger_link.server_context()

    def setup(self):
        if self.tls is not None:
            self.request.settimeout(TLS_HANDSHAKE_S)
            try:
                self.request = self.tls.wrap_socket(self.request, server_side=True)
            except OSError as exc:
                raise Handshake from exc
            self.request.settimeout(None)
        super().setup()

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/v1/"):
            caller = request_caller(getattr(self, "headers", None))
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.command} {self.path.partition('?')[0]} {caller}\n")
            return
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    @request_metrics.measured
    def handle_one_request(self) -> None:
        self.command, self.path, self.raw_requestline = None, "/", b""
        return super().handle_one_request()

    def send(self, code, body, ctype):
        if self.path.startswith("/api/v1/") and code >= 400 and ctype != "application/json":
            body = json.dumps({"error": {"code": "forbidden" if code == 403 else "request_refused", "message": body}})
            ctype = "application/json"
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self.path.startswith("/api/") and self.headers.get("Origin") == FILE_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", FILE_ORIGIN)
        self.end_headers()
        self.wfile.write(data)

    def send_header(self, keyword, value):
        super().send_header(keyword, value)
        if keyword.lower() == "content-type" and value.startswith("text/html"):
            for name, policy in PAGE_HEADERS.items():
                super().send_header(name, policy)

    def slug(self):
        return self.path.split("?", 1)[0].strip("/").removeprefix("api/").removesuffix(".html")

    def exists(self, slug):
        return core.SLUG_RE.match(slug) and repository.exists(slug)

    def refused(self, slug=None):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self.send(403, "host not allowed", "text/plain") or True
        if self.headers.get("Origin") not in (None, FILE_ORIGIN, *ALLOWED_ORIGINS):
            return self.send(403, "origin not allowed", "text/plain") or True
        if slug is not None:
            self.principal = authority.caller(self.headers, repository.token(slug), slug)
            if self.principal is None:
                return self.send(403, "missing or wrong ledger token", "text/plain") or True
        return False

    def reply_state(self, slug, changes=None, ops=None, refusals=None, screen=None):
        """Apply a page save and answer with a bounded acknowledgment; the page takes state from the event stream."""
        refusals = refusals or {}
        screen = screen or ledger_task_duplicates.Screen()
        gate = write_gate(slug) if ops else None
        try:
            state, rejected = repository.apply_ops(
                slug,
                changes=changes,
                ops=ops,
                gate=ledger_task_duplicates.Gate(screen, gate) if screen.refused else gate,
            )
        except (ValueError, OSError) as exc:
            return self.send(500, f"ledger unreadable: {exc}", "text/plain")
        if gate:
            gate.publish(rejected)
        if changes or ops:
            relay_to_inbox(slug, state)
            doctor_phrase(slug, state)
        deliver_alerts(slug, state)
        warnings = [*state["_meta"].get("warnings", []), *refusals.values(), *screen.warnings.values()]
        reply = {
            "applied": [op["id"] for op in ops or [] if op["id"] not in rejected],
            "rejected": [*rejected, *refusals],
            "_meta": {"rev": state["_meta"]["rev"], "warnings": [warning[:1000] for warning in warnings[:20]]},
        }
        return self.send(200, json.dumps(reply, ensure_ascii=False), "application/json")

    @request_metrics.measured
    def do_OPTIONS(self):
        self.send_response(204)
        if self.headers.get("Origin") == FILE_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", FILE_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, X-Ledger-Token, X-Ledger-Agent, X-Ledger-Slug"
            )
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    @request_metrics.measured
    @request_metrics.endpoint
    def do_GET(self):
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, api_services())
        route, slug = self.path.split("?", 1)[0], self.slug()
        if self.refused():
            return None
        if route == "/healthz":
            return self.send(200, json.dumps({"dir": str(core.LEDGER_DIR)}), "application/json")
        if route in ("/logo.png", "/favicon.ico"):
            return self.send_logo()
        if route.startswith("/media/"):
            return self.send_media(*route.removeprefix("/media/").partition("/")[::2])
        if route.startswith("/artifacts/"):
            return self.send_media(*route.removeprefix("/artifacts/").partition("/")[::2], store=ledger_artifacts)
        if route.startswith("/static/"):
            return self.send_static(route)
        if route == "/":
            return self.send(200, index_page(), "text/html; charset=utf-8")
        if route == "/api/layout":
            return self.send(200, json.dumps(ledger_layout.read()), "application/json")
        if route.startswith("/api/swarm/"):
            name, _, view = slug.removeprefix("swarm/").partition("/")
            return self.get_swarm(name, view)
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        if route.startswith("/api/"):
            return self.send(410, f"read the ledger from /api/v1/ledgers/{slug}", "text/plain")
        return self.send(200, page_for(slug), "text/html; charset=utf-8")

    def get_swarm(self, slug, view):
        if view not in ("", "models") or not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        if self.refused(slug):
            return None
        if view:
            return self.send(200, json.dumps(swarm_models()), "application/json")
        status = swarm_status(slug)
        if status is None:
            return self.send(404, "no swarm for this ledger", "text/plain")
        return self.send(200, json.dumps(status), "application/json")

    def put_swarm(self, slug):
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        if self.refused(slug):
            return None
        if self.principal:
            return self.send(403, "swarm controls need the operator", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body size out of range")
            body = core.loads(self.rfile.read(length) or b"{}")
            action = body.get("action") if isinstance(body, dict) else None
            if action == "lifecycle":
                control = functools.partial(lifecycle_control, slug, body, not self.principal)
            elif action == "quota_refresh":
                control = functools.partial(refresh_quota, slug)
            else:
                command, argv = ("doctor", DOCTOR[action]) if action in DOCTOR else ("swarm", control_argv(body))
                if refused := effort_refusal(slug, body):
                    raise ValueError(refused)
                control = functools.partial(swarm_control, slug, argv, command)
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        Refused = ports.swarm().lifecycle.Refused

        try:
            status, error = control()
        except Refused as refused:
            return self.send(refused.status, str(refused), "text/plain")
        if error:
            return self.send(502, error, "text/plain")
        return self.send(200, json.dumps(status), "application/json")

    def put_layout(self):
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            return self.send(403, "origin not allowed", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, "Content-Type must be application/json", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body size out of range")
            saved = ledger_layout.write(ledger_layout.loads(self.rfile.read(length)))
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        return self.send(200, json.dumps(saved), "application/json")

    def send_export(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_logo(self):
        try:
            data = LOGO.read_bytes()
        except OSError:
            return self.send(404, "no logo", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, route):
        match = STATIC_RE.fullmatch(route)
        version, assets = served_page()
        data = match and assets.get(match.group(2))
        if data is None or match.group(1) != version:
            return self.send(404, "no such asset", "text/plain")
        self.send_response(200)
        for keyword, value in {"Content-Type": STATIC_TYPES[match.group(3)], **ASSET_HEADERS}.items():
            self.send_header(keyword, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_media(self, slug, media_id, store=ledger_media):
        try:
            if not core.SLUG_RE.match(slug):
                raise ValueError("not a ledger")
            data = store.served(slug, media_id)
        except (ValueError, OSError):
            return self.send(404, "no such file", "text/plain")
        ctype = store.TYPES[media_id.rsplit(".", 1)[1]]
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
        )
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        if self.headers.get("Origin") == FILE_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", FILE_ORIGIN)
        self.end_headers()
        self.wfile.write(data)

    def post_media(self, slug):
        return self.receive(slug, ledger_media.MAX_BYTES, lambda data: ledger_media.store(slug, data))

    def post_artifact(self, slug):
        name = self.headers.get("X-Artifact-Name", "")

        def store(data):
            try:
                request = core.loads(self.headers.get("X-Artifact-Request", "{}"))
                if not isinstance(request, dict) or set(request) - {"task", "title", "request", "plan"}:
                    raise ValueError("artifact upload takes task, title and optional request or plan")
                op = {"op": "artifact_add", "by": self.headers.get("X-Ledger-Agent"), "task": "", **request}
                ledger_artifacts.check_add(
                    {**op, "id": "upload", "title": request.get("title", ""), "file": {"id": "0" * 64 + ".md"}}
                )
            except (ValueError, TypeError) as exc:
                raise ledger_media.Refused(400, str(exc)) from exc
            doc = repository.get_document(slug)
            reason = authority.refusal(self.principal, op) or ledger_artifacts.refusal(doc, op, doc["_meta"]["members"])
            if reason:
                raise ledger_media.Refused(403, reason)
            return ledger_artifacts.store(slug, name, data)

        return self.receive(slug, ledger_artifacts.MAX_BYTES, store, agents_only=True)

    def receive(self, slug, limit, store, agents_only=False):
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        origin = self.headers.get("Origin")
        agent = self.headers.get("X-Ledger-Agent")
        if origin not in ALLOWED_ORIGINS and (origin is not None or not agent):
            return self.send(403, "origin not allowed", "text/plain")
        if self.refused(slug):
            return None
        if agent or agents_only:
            meta = repository.read(slug, "_meta.members")["_meta"]
            if agent not in meta.get("members", {}):
                return self.send(403, "agent must join this ledger before uploading", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            self.close_connection = True
            return self.send(413, f"an upload may be at most {limit >> 20} MB", "text/plain")
        if length <= 0:
            return self.send(400, "the upload is empty", "text/plain")
        try:
            stored = store(self.rfile.read(length))
        except ledger_media.Refused as exc:
            return self.send(exc.status, str(exc), "text/plain")
        return self.send(200, json.dumps(stored), "application/json")

    def post_route(self, route: str) -> Callable[[], None] | None:
        if route == "/healthz":
            return self.probe_write
        for prefix, post in (("/api/media/", self.post_media), ("/api/artifacts/", self.post_artifact)):
            if route.startswith(prefix):
                return functools.partial(post, route.removeprefix(prefix))
        return None

    def probe_write(self) -> None:
        with core.LOCK:
            core.atomic_write(core.LEDGER_DIR / PROBE_FILE, str(core.now_ms()))
        return self.send(200, json.dumps({"dir": str(core.LEDGER_DIR)}), "application/json")

    @request_metrics.measured
    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, api_services())
        if self.refused():
            return None
        route = self.path.split("?", 1)[0]
        if post := self.post_route(route):
            return post()
        if route != "/api/bin":
            return self.send(404, "not found", "text/plain")
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            return self.send(403, "origin not allowed", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, "Content-Type must be application/json", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body size out of range")
            action, slug = bin_request(core.loads(self.rfile.read(length) or b"{}"))
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        reply = {}
        if action == "delete":
            ledger_bin.delete(slug)
            if swarm_status(slug) is not None:
                _, error = swarm_control(slug, ["stop", "--now"])
                reply = {"swarm_error": error} if error else {}
        elif not repository.restore(slug):
            return self.send(404, "not in the bin", "text/plain")
        return self.send(200, json.dumps({"binned": sorted(ledger_bin.entries()), **reply}), "application/json")

    @request_metrics.measured
    def do_PATCH(self):
        from scripts.swarm_ledger import api

        if not self.path.startswith("/api/v1/"):
            return self.send(404, "not found", "text/plain")
        return api.handle(self, api_services())

    @request_metrics.measured
    def do_PUT(self):
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, api_services())
        slug = self.slug()
        if self.refused():
            return None
        if self.path.split("?", 1)[0] == "/api/layout":
            return self.put_layout()
        if self.path.startswith("/api/swarm/"):
            return self.put_swarm(slug.removeprefix("swarm/"))
        if not self.path.startswith("/api/") or not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        return self.put_ledger(slug)

    def put_ledger(self, slug):
        if self.refused(slug):
            return None
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, "Content-Type must be application/json", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body size out of range")
            body = core.loads(self.rfile.read(length) or b"{}")
            state = repository.get_document(slug)
            task_ids = tuple(task["id"] for task in state.get("tasks", []))
            changes, ops = core.check_body(body, task_ids)
            ledger_media.resolve(slug, ops)
            ledger_artifacts.resolve(slug, ops)
        except ValueError as exc:
            return self.send(400, f'body must be {{"changes": [...], "ops": [...]}}: {exc}', "text/plain")
        if self.principal and changes:
            return self.send(403, "page changes need the operator", "text/plain")
        refusals = {op["id"]: text for op in ops if (text := authority.refusal(self.principal, op))}
        allowed = [op for op in ops if op["id"] not in refusals]
        return self.reply_state(slug, changes, allowed, refusals, ledger_task_duplicates.screen(state, allowed))


def metrics_journal(action, *args):
    try:
        action(*args)
    except OSError as exc:
        sys.stderr.write(f"request metrics journal: {exc}\n")


def container_main(proc=Path("/proc")) -> bool:
    try:
        return (proc / str(os.getppid()) / "comm").read_text().startswith("containerd-shim")
    except OSError:
        return False


def reload_if_changed(started, server, code_dirs=CODE_DIRS, hand_off=server_handoff.hand_off, force=False, loaded=None):
    """None once a successor took the socket and this server stopped accepting, else the code stamp to compare
    next; with loaded, code whose content still hashes to it reads as unchanged."""
    stamp = code_stamp(code_dirs)
    if stamp == started and not force:
        return started
    if loaded is not None and not force and code_digest(code_dirs) == loaded:
        server_code.record(PIDFILE.parent, os.getpid(), stamp, code_dirs)
        return stamp
    argv = [sys.executable, str(CODE_DIR / "ledger_server.py"), "--serve"]
    if container_main():
        metrics_journal(request_metrics.stop)
        try:
            os.execv(sys.executable, argv)
        except OSError as exc:
            sys.stderr.write(f"reload: could not restart in place: {exc}; this server keeps serving\n")
            metrics_journal(request_metrics.restore, core.LEDGER_DIR / request_metrics.JOURNAL_NAME)
            return stamp
    try:
        held = hand_off(server.socket, argv, extra={WATCHED: ",".join(HUB.watched()), MODULES: loaded_modules()})
    except OSError as exc:
        sys.stderr.write(f"reload: the successor did not start: {exc}; this server keeps serving\n")
        return stamp
    if held is None:
        sys.stderr.write("reload: the successor never reported ready; this server keeps serving\n")
        return stamp
    try:
        HANDOFF["since"] = json.loads(held)
    except ValueError:
        HANDOFF["since"] = {}
    server.shutdown()
    return None


def write_carry(since: dict) -> None:
    path = core.LEDGER_DIR / CARRY
    staged = path.with_name(f"{CARRY}.{os.getpid()}")
    staged.write_text(json.dumps(stored.carry(since)))
    staged.replace(path)


def take_carry() -> list[str] | None:
    """The slugs whose committed copies the old server carried over."""
    path = core.LEDGER_DIR / CARRY
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    path.unlink()
    try:
        carried = json.loads(text)
        stored.adopt_carried(carried)
    except Exception as exc:  # a broken carry leaves its ledgers to load from their rows
        sys.stderr.write(f"carry: {exc}\n")
        return None
    return list(carried)


def refresh_streams(slugs: list[str]) -> None:
    """Bring a warmed stream view up to a ledger the old server wrote after the successor built it."""
    for slug in slugs:
        if HUB.has(slug):
            try:
                publish_ledger(slug, repository.get_document(slug))
            except Exception as exc:  # the next write to the ledger publishes its view anyway
                sys.stderr.write(f"refresh stream {slug}: {exc}\n")


def request_reload(signum: int, frame: object) -> None:
    RELOAD.set()


def hand_over(started: int, server: ThreadingHTTPServer, force: bool = False) -> int | None:
    with HANDING:
        if HANDED.is_set():
            return None
        started = reload_if_changed(started, server, force=force, loaded=LOADED_DIGEST)
        if started is None:
            HANDED.set()
        return started


def answer_reloads(server: ThreadingHTTPServer, requested: threading.Event = RELOAD) -> None:
    while requested.wait():
        requested.clear()
        if hand_over(code_stamp(), server, force=True) is None:
            return


def reloading(environ=os.environ) -> bool:
    return environ.get("SWARM_RELOAD") == "1"


def scheduled_wait(interval: float) -> None:
    from redis.exceptions import NoPermissionError

    from scripts.scheduler.process_timer import wait

    if not SCHEDULER_UNAVAILABLE.is_set():
        try:
            wait(interval)
            return
        except (OSError, NoPermissionError) as exc:
            SCHEDULER_UNAVAILABLE.set()
            sys.stderr.write(f"scheduler wait unavailable, sleeping instead: {exc}\n")
    time.sleep(interval)


def watch_ledgers(server=None, interval=2.0, stopping=NEVER):
    started = LOADED_STAMP
    reloads = reloading()
    for passes in itertools.count():
        if reloads:
            started = hand_over(started, server)
            if started is None:
                return
        with WATCH_PASS:
            if stopping.is_set():
                return
            try:
                ledger_bin.tidy()
            except OSError as exc:
                sys.stderr.write(f"bin purge: {exc}\n")
            if passes % BIN_SWEEP_EVERY == 0:
                bin_closed_without_swarm()
            if passes and passes % BIN_SWEEP_EVERY == 0:
                expire_alerts()
            if passes % REPLAY_EVERY == 0:
                try:
                    if unrelayed():
                        replay_unrelayed()
                except Exception as exc:  # the watch loop also tidies the bin, expires alerts and samples streams
                    sys.stderr.write(f"unrelayed replay: {exc}\n")
            sample_streams()
            metrics_journal(request_metrics.persist)
        scheduled_wait(interval)


def listening(listener: socket.socket | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, PORT), Handler, bind_and_activate=False)
    server.request_queue_size = 128
    if listener is None:
        server.server_bind()
        server.server_activate()
    else:
        server.socket.close()
        server.socket = listener
    return server


def warm(passes: int = WARM_PASSES) -> None:
    """Load every open ledger, again while a pass still finds one written since, at most passes times."""
    held = {}
    for _ in range(passes):
        loaded = {}
        for summary in ledger_summaries():
            slug = summary["slug"]
            try:
                loaded[slug] = stored.current(slug)
            except Exception as exc:  # a ledger that fails to load stays cold and the handoff goes on
                sys.stderr.write(f"warm {slug}: {exc}\n")
        if all(held.get(slug) is entry for slug, entry in loaded.items()):
            return
        held.update(loaded)


def loaded_modules(modules=sys.modules) -> str:
    return ",".join(
        sorted(
            name
            for name, module in modules.items()
            if module is not None and name != "__main__" and name.rpartition(".")[2] != "ledger_server"
        )
    )


def warm_modules(environ=os.environ, load=importlib.import_module) -> None:
    for name in filter(None, environ.pop(MODULES, "").split(",")):
        try:
            load(name)
        except Exception:  # a module the new code no longer has stays unloaded
            continue


def warm_streams(environ=os.environ) -> list[str]:
    warmed = []
    for slug in filter(None, environ.pop(WATCHED, "").split(",")):
        try:
            HUB.open(slug, lambda slug=slug: stream_resources(slug))
        except Exception as exc:  # a view that fails to build loads on the page's own reconnect
            sys.stderr.write(f"warm stream {slug}: {exc}\n")
            continue
        HUB.close(slug)
        warmed.append(slug)
    return warmed


def follow_shared(interval=FOLLOW_S):
    """Publish each watched ledger's newer stored generation, whichever host wrote it to the shared database."""
    while True:
        for slug in HUB.watched():
            try:
                repository.current(slug)
            except Exception as exc:  # one unreadable ledger must not stop the others following
                sys.stderr.write(f"shared follow {slug}: {exc}\n")
        scheduled_wait(interval)


def retrying(target, sleep=time.sleep, first=1.0, cap=30.0):
    @functools.wraps(target)
    def run(*args, **kwargs):
        delay = first
        while True:
            try:
                return target(*args, **kwargs)
            except Exception:  # an unresolved Redis name at pod start must not end the thread for good
                sys.stderr.write(f"{target.__name__} stopped, retrying in {delay:g}s: {sys.exc_info()[1]}\n")
                sleep(delay)
                delay = min(delay * 2, cap)

    return run


def adopt_disk_files():
    for folder in sorted(core.LEDGER_DIR.glob("*.media")):
        slug = folder.name.removesuffix(".media")
        try:
            ledger_artifacts.adopt_disk(slug)
        except Exception as exc:  # one unreadable ledger must not stop the others adopting
            sys.stderr.write(f"disk adoption {slug}: {exc}\n")


def take_lease(writer, ledger_writer, listener, predecessor=os.getppid) -> None:
    """A successor outwaits the predecessor that handed it the socket, since exiting would leave nothing listening."""
    while True:
        try:
            writer.acquire(ledger_writer.owner(), wait=0.0 if listener is None else LEASE_WAIT_S)
            return
        except ledger_writer.WriterConflict as conflict:
            held = conflict.holder.get("pid")
            if listener is None or held != predecessor():
                sys.exit(str(conflict))
            sys.stderr.write(f"reload: predecessor pid {held} still holds the writer lease; waiting\n")


def serve():
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    listener = server_handoff.inherited()
    warmed, carried = [], []
    if listener is not None:
        warm_modules()
        warm()
        warmed = warm_streams()
        server_handoff.announce(json.dumps(stored.generations()).encode())
    ledger_writer = ports.swarm().ledger_writer
    writer = ledger_writer.WriterLease(core.LEDGER_DIR)
    if not storage.shared():
        take_lease(writer, ledger_writer, listener)
    if listener is not None:
        carried = take_carry()
    legacy.adopt(stored)
    adopt_unrelayed()
    threading.Thread(target=adopt_disk_files, daemon=True).start()
    metrics_journal(request_metrics.restore, core.LEDGER_DIR / request_metrics.JOURNAL_NAME)
    server = listening(listener)
    watching = threading.Event()
    threading.Thread(target=retrying(watch_ledgers), args=(server,), kwargs={"stopping": watching}, daemon=True).start()
    threading.Thread(target=answer_reloads, args=(server,), daemon=True).start()
    threading.Thread(target=refresh_streams, args=(warmed if carried is None else carried,), daemon=True).start()
    if storage.shared():
        threading.Thread(target=retrying(follow_shared), daemon=True).start()
    stopped = server_lifetime.watch(server, core.LEDGER_DIR, PORT)
    server_code.record(PIDFILE.parent, os.getpid(), LOADED_STAMP, digest=LOADED_DIGEST)
    PIDFILE.write_text(str(os.getpid()))
    print(f"ledger server on {BASE}, dir {core.LEDGER_DIR}", flush=True)
    drained = True
    try:
        from scripts.hub import projector
        from scripts.swarm_ledger import outbox_host

        with outbox_host.running(stored), projector.running(repository, HUB):
            try:
                server.serve_forever(POLL_S)
            finally:
                stopped.set()
                HUB.drain()
                drained = server.drained(DRAIN_S)
    finally:
        server.server_close()
        with WATCH_PASS:
            watching.set()
        metrics_journal(request_metrics.stop)
        if drained:
            try:
                if "since" in HANDOFF:
                    write_carry(HANDOFF.pop("since"))
            finally:
                writer.release()
        else:
            sys.stderr.write("drain: requests still ran at the bound; the lease ends with this process\n")


def port_held() -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, PORT))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            return True
    return False


def server_process_alive() -> bool:
    try:
        pid = int(PIDFILE.read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    if sys.platform != "linux":
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return not cmdline or b"ledger_server.py" in cmdline


def wait_for(folder: Path) -> float:
    return COMPOSE_WAIT if server_compose.used(folder) else SERVER_WAIT


def start_server(compose: bool) -> bool:
    if compose:
        if error := server_compose.start(core.LEDGER_DIR):
            sys.exit(f"could not start the ledger service: {error}")
        return True
    if port_held() or server_process_alive():
        return False
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    core.rotate_if_full(LOGFILE)
    with open(LOGFILE, "a") as log:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={
                **server_lifetime.environment(core.LEDGER_DIR, PORT),
                "SWARM_RELOAD": os.environ.get("SWARM_RELOAD", "1"),
            },
        )
    return True


def ensure():
    deadline = time.monotonic() + wait_for(core.LEDGER_DIR)
    compose = server_compose.used(core.LEDGER_DIR)
    started = False
    running = ledger_link.serving(url=BASE)
    while running is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            sys.exit(f"ledger server did not answer on {BASE}; see {LOGFILE} or docker compose logs ledger")
        if not started:
            started = start_server(compose)
        time.sleep(min(0.1, remaining))
        running = ledger_link.serving(timeout=min(1, remaining), url=BASE)
    if running != str(core.LEDGER_DIR):
        sys.exit(
            f"{BASE} already serves {running or 'no ledger folder'}, not {core.LEDGER_DIR}; stop that ledger server first"
        )
    print(BASE)


def stop():
    if server_compose.used(core.LEDGER_DIR) and (error := server_compose.stop(core.LEDGER_DIR)):
        sys.exit(f"could not stop the ledger service: {error}")
    if not PIDFILE.exists():
        return print(f"no server pidfile in {core.LEDGER_DIR}")
    pid = int(PIDFILE.read_text())
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        cmdline = b""
    if b"ledger_server.py" in cmdline:
        os.kill(pid, signal.SIGTERM)
    PIDFILE.unlink()
    print("stopped")


def recorded_pid() -> str:
    try:
        return PIDFILE.read_text()
    except OSError:
        return ""


def reload(wait: float = RELOAD_WAIT_S) -> str | None:
    if server_compose.used(core.LEDGER_DIR) and (error := server_compose.start(core.LEDGER_DIR)):
        sys.exit(f"could not start the ledger service: {error}")
    if not server_process_alive():
        return ensure()
    pid = recorded_pid()
    try:
        os.kill(int(pid), signal.SIGHUP)
    except (ValueError, ProcessLookupError):
        return ensure()
    deadline = time.monotonic() + wait
    while all((recorded_pid() == pid, server_process_alive())):
        if time.monotonic() >= deadline:
            sys.exit(f"the ledger server pid {pid} did not hand off within {wait:.0f} s; see {LOGFILE}")
        time.sleep(0.1)
    return ensure()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ensure", action="store_true")
    group.add_argument("--serve", action="store_true")
    group.add_argument("--stop", action="store_true")
    group.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    composition.wire()
    if args.serve:
        signal.signal(signal.SIGHUP, request_reload)
        serve()
    elif args.ensure:
        ensure()
    elif args.reload:
        reload()
    else:
        stop()


if __name__ == "__main__":
    main()
