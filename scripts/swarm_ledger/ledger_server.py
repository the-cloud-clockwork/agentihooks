#!/usr/bin/env python3
"""Local ledger server: serves the ledger pages and reads/writes the ledger records in ~/development-ledger.

Usage:
  ledger_server.py --ensure   start it detached if it is not answering, print the base URL
  ledger_server.py --serve    run in the foreground
  ledger_server.py --stop     stop the detached server

Env: LEDGER_DIR (default ~/development-ledger), LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765),
SWARM_PUBLIC_URL and SWARM_ALLOWED_HOSTS (comma list) beside loopback, SWARM_RELOAD=1 for code reload
(--ensure sets it unless given), LEDGER_IMPECCABLE_LIVE=1 for the Impeccable live origin on a scratch server.
Idempotent: --ensure on a running server only prints the URL.
"""

import argparse
import errno
import functools
import html
import itertools
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from scripts.swarm_ledger import ledger_task_duplicates, page_policy, server_lifetime  # noqa: E402
from scripts.swarm_ledger.events import Hub  # noqa: E402
from scripts.swarm_ledger.events.publishing import publishing  # noqa: E402
from scripts.swarm_ledger.repository import legacy  # noqa: E402
from scripts.swarm_ledger.repository import repository as stored  # noqa: E402

HOST, PORT = ledger_link.address()
BASE = f"http://{HOST}:{PORT}"
PIDFILE = core.LEDGER_DIR / ".server.pid"
LOGFILE = core.LEDGER_DIR / ".server.log"
SERVER_WAIT = 5.0
FILE_ORIGIN = "null"
MAX_BODY = 1 << 20
ALLOWED_HOSTS = ledger_link.allowed_hosts()
ALLOWED_ORIGINS = ledger_link.allowed_origins()
CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
LOGO = ROOT / "media" / "agentihooks-logo.png"
CODE_DIRS = (
    CODE_DIR,
    *(ROOT / "scripts" / name for name in ("inbox", "swarm", "handoff", "doctor", "gates", "hive")),
    ROOT / "hooks",
)


HUB = Hub()
BIN_SWEEP_EVERY = 15


@functools.cache
def served_page():
    """The assets this process serves and their version, read once so a version URL never serves other bytes."""
    assets = {name: path.read_bytes() for name, path in core.static_assets().items()}
    return core.page_version(assets), assets


def served_version():
    return served_page()[0]


def ledger_view(state):
    meta = {key: item for key, item in state["_meta"].items() if key not in ("seeds", "api_operations")}
    meta.update(page_version=served_version(), crew=ledger_gate.crew(state["_meta"]))
    return json.loads(json.dumps({**state, "_meta": meta}))


def publish_ledger(slug, state):
    if HUB.has(slug):
        HUB.publish(slug, "ledger", ledger_view(state))


repository = publishing(stored, publish_ledger)


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


ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">{}</svg>'
)
TRASH = ICON.format(
    '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/><path d="M10 11v6M14 11v6"/>'
)
HOME_ICON = ICON.format('<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>')
HEADS = {
    "home": ("", "Ledger", "Kind:kind", "Overview", ">Open:open", ">Done:done", "Swarm:swarm", ">Activity:at", ""),
    "bin": ("Ledger", "Kind", "Overview", ">Deleted", ">Left", ""),
}
PAGE_POLICY = page_policy.policy(os.environ, core.LEDGER_DIR, PORT)
STATIC_RE = re.compile(r"/static/([0-9a-f]{12})/((?:[a-z]+/)?[a-z_]+\.(js|css))")
STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
ASSET_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}
PAGE_HEADERS = {"Content-Security-Policy": PAGE_POLICY, "X-Content-Type-Options": "nosniff"}


def home_summaries():
    return [{**s, "swarm": swarm_state(s["slug"])} for s in ledger_summaries()]


FOLD_ALL = '<button class="act toggle-all" id="fold-all" type="button">Expand all</button>'


def head_cell(label):
    label, _, key = label.partition(":")
    css = ' class="r"' if label.startswith(">") else ""
    label = label.removeprefix(">")
    if not key:
        return f"<span{css}>{label}</span>"
    css = ' class="sort r"' if css else ' class="sort"'
    return f'<button{css} type="button" data-sort="{key}">{label}<i aria-hidden="true">&#8597;</i></button>'


def index_page(view="home"):
    """The HOME or BIN shell; its rows load from the v1 ledger and bin collections."""
    if view == "bin":
        heading, total, watermark = "BIN", '<span class="total" id="total"></span>', ""
        fab = f'<a class="fab" id="home-fab" href="/" aria-label="HOME">{HOME_ICON}</a>'
    else:
        heading, total, watermark = "HOME", FOLD_ALL, '<div class="watermark" aria-hidden="true"></div>'
        fab = f'<a class="fab" id="bin-fab" href="/?view=bin" aria-label="Bin">{TRASH}</a>'
    values = {
        "HEADING": heading,
        "PAGE": served_version(),
        "WATERMARK": watermark,
        "VIEW": view,
        "TOTAL": total,
        "HEAD": "".join(head_cell(label) for label in HEADS[view]),
        "FAB": fab,
    }
    return re.sub(
        r"__HOME_(HEADING|PAGE|WATERMARK|VIEW|TOTAL|HEAD|FAB)__",
        lambda m: values[m.group(1)],
        core.HOME.read_text(encoding="utf-8"),
    )


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
    from scripts.swarm.store import connect

    return connect()


def swarm_status(slug, state=None):
    from scripts.swarm import commands
    from scripts.swarm.store import SwarmError

    try:
        return commands.view(swarm_store(), slug)
    except SwarmError:
        return None
    except Exception as exc:
        sys.stderr.write(f"swarm status {slug}: {exc}\n")
        return None


def swarm_state(slug):
    from scripts.swarm.store import SwarmError

    try:
        return swarm_store().config(slug).state
    except SwarmError:
        return None
    except Exception as exc:  # HOME still renders; the log keeps why this read failed
        sys.stderr.write(f"swarm state {slug}: {exc}\n")
        return None


def bin_closed_without_swarm(now=None):
    from scripts.swarm.store import SwarmError

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
EFFORTS = ("low", "medium", "high", "max")
SCALING = ("auto", "manual")
MAX_LOAD = 10.0
MASTER_AGENTS = ("claude", "codex")
MAX_NOTE = 500
FINDING_RE = re.compile(r"^[a-z][a-z-]*/[\w.-]{1,64}$")


def control_argv(body):
    action = body.get("action") if isinstance(body, dict) else None
    if action in CONTROLS:
        return CONTROLS[action]
    if action == "terminate":
        name = body.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9@._-]{1,200}", name) or name.startswith("-"):
            raise ValueError("terminate needs an exact agent name")
        return ["terminate", name]
    if action == "verdict":
        return verdict_argv(body)
    if action == "restore-decision":
        return restore_decision_argv(body)
    if action == "lift":
        return lift_argv(body)
    if action != "set":
        raise ValueError("action must be start, pause, stop, stop_now, close, reopen, set or verdict")
    pairs = []
    for key, flag, limit in (
        ("max_eng", "max-eng-agents", MAX_CAP),
        ("max_ci", "max-ci-agents", MAX_CAP),
        ("max_plan", "max-plan-agents", MAX_CAP),
        ("compact_limit", "compact-limit", MAX_COMPACT),
    ):
        value = body.get(key)
        if value is None:
            continue
        low = MIN_COMPACT if key == "compact_limit" else 0
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
            "set needs max_eng, max_ci, max_plan, compact_limit, effort_min, effort_max, autonomy, "
            "scaling, load_high, load_low, memory_per_agent_mb, master_agent, overlays or gates"
        )
    return ["set", *pairs]


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
    from hooks.context.profile_chain import OVERLAY_CAP
    from scripts.swarm.overlays import KEY, ROLES

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
    if choice not in {"resume", "fresh"}:
        raise ValueError("Choose resume or fresh")
    return ["restore-decision", agent, choice]


def lift_argv(body):
    agent, gate = body.get("agent"), body.get("gate")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9@_.-]{0,127}", agent):
        raise ValueError("A lift needs an agent name")
    if not isinstance(gate, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", gate):
        raise ValueError("A lift needs the gate's name")
    return ["lift", agent, gate]


def verdict_argv(body):
    from scripts.swarm.health.verdicts import VERDICTS

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
    from scripts.swarm import commands

    store = swarm_store()
    if not any(agent.name == name for agent in store.agents(slug)):
        return None, "agent is not in this swarm"
    commands.submit(store, slug, "swarm", ["terminate", name])
    return swarm_status(slug), ""


def swarm_control(slug, argv, command="swarm"):
    from scripts.swarm import commands
    from scripts.swarm.store import SwarmError

    try:
        if command == "swarm" and argv[0] == "terminate":
            return terminate_control(slug, argv[1])
        commands.submit(swarm_store(), slug, command, argv)
        return swarm_status(slug), ""
    except SwarmError as exc:
        return None, str(exc)


def refresh_quota(slug: str) -> tuple[dict | None, str]:
    return swarm_control(slug, [], "quota")


def relay_to_inbox(slug, state):
    """Operator writes from this sync become inbox items for the swarm on this ledger, if it has one."""
    meta = state["_meta"]
    events = [e for e in meta.get("events", []) if e.get("rev") == meta["rev"] and e.get("by") == "operator"]
    if not events:
        return []
    try:
        import watch_ledger

        from scripts.inbox.store import connect
        from scripts.swarm import operator_mail
        from scripts.swarm.store import RedisStore

        inbox = connect()
        return operator_mail.relay(inbox, RedisStore(inbox.redis), slug, state, events, watch_ledger.line)
    except Exception as exc:  # the ledger write stands whatever the inbox does
        sys.stderr.write(f"inbox relay for {slug}: {exc}\n")
        return []


def deliver_alerts(slug, state):
    meta = state["_meta"]
    if not any(a.get("rev") == meta["rev"] for a in state.get("alerts", [])):
        return []
    try:
        from scripts.inbox.store import connect
        from scripts.swarm import operator_mail
        from scripts.swarm.store import RedisStore

        inbox = connect()
        live = [a for a in RedisStore(inbox.redis).agents(slug) if a.state != "finished"]
        master = operator_mail.master_address(slug, live)
        from scripts.inbox.seats import seat_address

        addresses = {a.name: seat_address(slug, a.seat) if a.seat else a.name for a in live}
        alerts = [{**a, "target": addresses.get(a["target"], a["target"])} for a in state["alerts"]]
        return ledger_alerts.deliver(inbox, slug, alerts, meta["rev"], master)
    except Exception as exc:  # the ledger write stands whatever the inbox does
        sys.stderr.write(f"alert delivery for {slug}: {exc}\n")
        return []


def expire_alerts() -> None:
    at = core.now_ms()
    for summary in ledger_summaries():
        slug = summary["slug"]
        state = repository.get_document(slug)
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
    ledger = ledger_view(repository.get_document(slug))
    return {"ledger": ledger, "swarm": swarm_status(slug, ledger)}


def workspace_tails(slug, task_id):
    """The latest work folder lines the hive tick published for one task, read when its proof fold opens."""
    from scripts.swarm import commands
    from scripts.swarm.store import SwarmError

    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError(f"no work folder for task {task_id!r}")
    try:
        return commands.workspaces(swarm_store(), slug).get(task_id, {})
    except SwarmError:
        return {}


def sample_streams():
    """Publish swarm status for every ledger a stream is open on."""
    HUB.evict()
    for slug in HUB.watched():
        ledger = HUB.resource(slug, "ledger")
        if ledger is None:
            continue
        try:
            HUB.publish(slug, "swarm", swarm_status(slug, ledger))
        except Exception as exc:  # the seed watcher that calls this must outlive any one ledger's readers
            sys.stderr.write(f"stream sample {slug}: {exc}\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.path.startswith("/api/v1/"):
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.command} {self.path.split('?', 1)[0]}\n")
            return
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

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
            self.principal = authority.principal(
                repository.token(slug),
                slug,
                self.headers.get("X-Ledger-Token"),
                self.headers.get("X-Ledger-Agent"),
            )
            if self.principal is None:
                return self.send(403, "missing or wrong ledger token", "text/plain") or True
        return False

    def reply_state(self, slug, changes=None, ops=None, refusals=None, screen=None):
        """Apply a page save and answer with a bounded acknowledgment; the page takes state from the event stream."""
        refusals = refusals or {}
        screen = screen or ledger_task_duplicates.Screen()
        gate = talk.Budget(slug) if ops else None
        try:
            state, rejected = repository.apply_ops(
                slug,
                changes=changes,
                ops=ops,
                gate=ledger_task_duplicates.Gate(screen, gate) if screen.refused else gate,
            )
        except (ValueError, OSError) as exc:
            return self.send(500, f"ledger unreadable: {exc}", "text/plain")
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

    def do_OPTIONS(self):
        self.send_response(204)
        if self.headers.get("Origin") == FILE_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", FILE_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ledger-Token, X-Ledger-Agent")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, sys.modules[__name__])
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
            view = "bin" if "view=bin" in self.path.partition("?")[2].split("&") else "home"
            return self.send(200, index_page(view), "text/html; charset=utf-8")
        if route == "/api/layout":
            return self.send(200, json.dumps(ledger_layout.read()), "application/json")
        if route.startswith("/api/swarm/"):
            slug = slug.removeprefix("swarm/")
            if not self.exists(slug):
                return self.send(404, "no such ledger", "text/plain")
            if self.refused(slug):
                return None
            status = swarm_status(slug)
            if status is None:
                return self.send(404, "no swarm for this ledger", "text/plain")
            return self.send(200, json.dumps(status), "application/json")
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        if route.startswith("/api/"):
            return self.send(410, f"read the ledger from /api/v1/ledgers/{slug}", "text/plain")
        return self.send(200, page_for(slug), "text/html; charset=utf-8")

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
            if action == "quota_refresh":
                control = functools.partial(refresh_quota, slug)
            else:
                command, argv = ("doctor", DOCTOR[action]) if action in DOCTOR else ("swarm", control_argv(body))
                control = functools.partial(swarm_control, slug, argv, command)
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        status, error = control()
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
            data = store.path_of(slug, media_id).read_bytes()
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

    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, sys.modules[__name__])
        if self.refused():
            return None
        route = self.path.split("?", 1)[0]
        if route.startswith("/api/media/"):
            return self.post_media(route.removeprefix("/api/media/"))
        if route.startswith("/api/artifacts/"):
            return self.post_artifact(route.removeprefix("/api/artifacts/"))
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
        elif not ledger_bin.restore(slug):
            return self.send(404, "not in the bin", "text/plain")
        return self.send(200, json.dumps({"binned": sorted(ledger_bin.entries()), **reply}), "application/json")

    def do_PUT(self):
        if self.path.startswith("/api/v1/"):
            from scripts.swarm_ledger import api

            return api.handle(self, sys.modules[__name__])
        slug = self.slug()
        if self.refused():
            return None
        if self.path.split("?", 1)[0] == "/api/layout":
            return self.put_layout()
        if self.path.startswith("/api/swarm/"):
            return self.put_swarm(slug.removeprefix("swarm/"))
        if not self.path.startswith("/api/") or not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
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


def code_stamp(code_dirs=CODE_DIRS):
    return max(
        (p.stat().st_mtime_ns for d in code_dirs for p in d.rglob("*") if p.suffix in (".py", ".html", ".js", ".css")),
        default=0,
    )


def reload_if_changed(started, code_dirs=CODE_DIRS, execv=os.execv):
    if code_stamp(code_dirs) == started:
        return False
    execv(sys.executable, [sys.executable, str(CODE_DIR / "ledger_server.py"), "--serve"])
    return True


def reloading(environ=os.environ) -> bool:
    return environ.get("SWARM_RELOAD") == "1"


def watch_ledgers(interval=2.0):
    started = code_stamp()
    reload = reloading()
    for passes in itertools.count():
        if reload:
            reload_if_changed(started)
        try:
            ledger_bin.tidy()
        except OSError as exc:
            sys.stderr.write(f"bin purge: {exc}\n")
        if passes % BIN_SWEEP_EVERY == 0:
            bin_closed_without_swarm()
            expire_alerts()
        sample_streams()
        time.sleep(interval)


def serve():
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    legacy.adopt(stored)
    threading.Thread(target=watch_ledgers, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    stopped = server_lifetime.watch(server, core.LEDGER_DIR, PORT)
    PIDFILE.write_text(str(os.getpid()))
    print(f"ledger server on {BASE}, dir {core.LEDGER_DIR}", flush=True)
    try:
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()


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


def ensure():
    deadline = time.monotonic() + SERVER_WAIT
    started = False
    running = ledger_link.serving(url=BASE)
    while running is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            sys.exit(f"ledger server did not answer on {BASE}; see {LOGFILE}")
        if not started and not port_held() and not server_process_alive():
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
            started = True
        time.sleep(min(0.1, remaining))
        running = ledger_link.serving(timeout=min(1, remaining), url=BASE)
    if running != str(core.LEDGER_DIR):
        sys.exit(
            f"{BASE} already serves {running or 'no ledger folder'}, not {core.LEDGER_DIR}; stop that ledger server first"
        )
    print(BASE)


def stop():
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ensure", action="store_true")
    group.add_argument("--serve", action="store_true")
    group.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve()
    elif args.ensure:
        ensure()
    else:
        stop()


if __name__ == "__main__":
    main()
