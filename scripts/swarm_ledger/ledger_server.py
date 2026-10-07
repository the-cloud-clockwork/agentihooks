#!/usr/bin/env python3
"""Local ledger server: serves ~/development-ledger/<slug>.html and reads/writes <slug>.json.

Usage:
  ledger_server.py --ensure   start it detached if it is not answering, print the base URL
  ledger_server.py --serve    run in the foreground
  ledger_server.py --stop     stop the detached server

Env: LEDGER_DIR (default ~/development-ledger), LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765).
Idempotent: --ensure on a running server only prints the URL.
"""

import argparse
import errno
import functools
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
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
import ledger_workspace  # noqa: E402
import new_ledger  # noqa: E402

from scripts.gates import talk  # noqa: E402
from scripts.swarm_ledger.events import Hub  # noqa: E402
from scripts.swarm_ledger.events.publishing import Publishing  # noqa: E402
from scripts.swarm_ledger.repository import repository as stored  # noqa: E402

HOST, PORT = ledger_link.address()
BASE = f"http://{HOST}:{PORT}"
PIDFILE = core.LEDGER_DIR / ".server.pid"
LOGFILE = core.LEDGER_DIR / ".server.log"
SERVER_WAIT = 5.0
FILE_ORIGIN = "null"
MAX_BODY = 1 << 20
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
LOGO = ROOT / "media" / "agentihooks-logo.png"
HOME_PAGE = CODE_DIR / "home.html"
MODULE_RE = re.compile(r"/static/([0-9a-f]{12})/js/([a-z]+)\.js")
CODE_DIRS = (
    CODE_DIR,
    *(ROOT / "scripts" / name for name in ("inbox", "swarm", "handoff", "doctor", "gates")),
    ROOT / "hooks",
)


HUB = Hub()
TAIL_MARKS = {}


@functools.cache
def served_version():
    """The page version of this process: code_stamp re-execs the server when a page asset changes."""
    return core.page_version()


def ledger_view(state):
    meta = {key: item for key, item in state["_meta"].items() if key not in ("seeds", "api_operations")}
    meta.update(page_version=served_version(), crew=ledger_gate.crew(state["_meta"]))
    return json.loads(json.dumps({**state, "_meta": meta}))


def publish_ledger(slug, state):
    if HUB.has(slug):
        HUB.publish(slug, "ledger", ledger_view(state))


repository = Publishing(stored, publish_ledger)


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
RESTORE = ICON.format('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>')
HOME_ICON = ICON.format('<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>')


def ledger_row(s, cells, control, lead="", attrs=""):
    slug, title, overview = html.escape(s["slug"]), html.escape(s["title"]), html.escape(s["overview"])
    return (
        f'<li class="row"{attrs}>{lead}<a class="title" href="/{slug}" title="{title}">{title}</a>'
        f'<span class="kind">{html.escape(s["size"])}</span><span class="ov" title="{overview}">{overview}</span>'
        f'{cells}<span class="acts">{control.format(slug=slug, title=title)}</span></li>'
    )


def ago(at, now):
    minutes = (now - at) // 60000
    for unit, size in (("d", 1440), ("h", 60), ("m", 1)):
        if minutes >= size:
            return f"{minutes // size}{unit} ago"
    return "just now"


def activity(at, now):
    if not at:
        return '<span class="when">unknown</span>'
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at / 1000))
    local = time.strftime("%Y-%m-%d %H:%M", time.localtime(at / 1000))
    return f'<time class="when" datetime="{stamp}" title="{local}">{ago(at, now)}</time>'


def home_cells(s, state, now):
    state = "closed" if s["closed_at"] else state
    label, css = html.escape(state or "no swarm"), html.escape(state or "none")
    return (
        f'<span class="num open"><b>{s["open"]}</b> open</span><span class="num done"><b>{s["done"]}</b> done</span>'
        f'<span class="state s-{css}">{label}</span>{activity(s["updated_at"], now)}'
    )


SWARM_RANK = {"running": 0, "paused": 1, "stopping": 1, "drained": 2, "stopped": 3, "closed": 5}
FOLD = '<button class="fold" type="button" aria-expanded="false" aria-label="Show all of {title}">&#9656;</button>'


def swarm_rank(state):
    return SWARM_RANK.get(state, 4)


def home_row(s, state, now):
    state = "closed" if s["closed_at"] else state
    attrs = (
        f' data-slug="{html.escape(s["slug"])}" data-kind="{html.escape(s["size"])}" data-open="{s["open"]}"'
        f' data-done="{s["done"]}" data-swarm="{swarm_rank(state)}" data-at="{s["updated_at"] or 0}"'
    )
    control = (REOPEN if s["closed_at"] else "") + DELETE
    return ledger_row(s, home_cells(s, state, now), control, FOLD.format(title=html.escape(s["title"])), attrs)


def bin_cells(s):
    deleted = time.strftime("%Y-%m-%d", time.localtime(s["deleted_at"] / 1000))
    days = s["days_left"]
    return f'<span class="deleted">{deleted}</span><span class="left">{days} day{"" if days == 1 else "s"} left</span>'


HEADS = {
    "home": ("", "Ledger", "Kind:kind", "Overview", ">Open:open", ">Done:done", "Swarm:swarm", ">Activity:at", ""),
    "bin": ("Ledger", "Kind", "Overview", ">Deleted", ">Left", ""),
}
DELETE = (
    '<button class="act del" type="button" data-act="delete" data-slug="{slug}" '
    f'title="Move to the bin" aria-label="Move {{title}} to the bin">{TRASH}</button>'
)
REOPEN = (
    '<button class="act reopen" type="button" data-act="reopen" data-slug="{slug}" '
    'aria-label="Reopen {title}">Reopen</button>'
)
RESTORE_BUTTON = (
    '<button class="act restore" type="button" data-act="restore" data-slug="{slug}" '
    f'title="Restore to HOME" aria-label="Restore {{title}} to HOME">{RESTORE}Restore</button>'
)


FOLD_ALL = '<button class="act toggle-all" id="fold-all" type="button">Expand all</button>'


def head_cell(label):
    label, _, key = label.partition(":")
    css = ' class="r"' if label.startswith(">") else ""
    label = label.removeprefix(">")
    if not key:
        return f"<span{css}>{label}</span>"
    css = ' class="sort r"' if css else ' class="sort"'
    return f'<button{css} type="button" data-sort="{key}">{label}<i aria-hidden="true">&#8597;</i></button>'


def index_page(view="home", now=None):
    now = core.now_ms() if now is None else now
    if view == "bin":
        heading, empty = "BIN", "The bin is empty."
        rows = [ledger_row(s, bin_cells(s), RESTORE_BUTTON) for s in bin_summaries(now)]
        total = f'<span class="total">{len(rows)} ledger{"" if len(rows) == 1 else "s"}</span>'
        watermark = ""
        fab = f'<a class="fab" id="home-fab" href="/" title="HOME" aria-label="HOME">{HOME_ICON}</a>'
    else:
        heading, empty = "HOME", "No ledgers yet."
        total, watermark = FOLD_ALL, '<div class="watermark" aria-hidden="true"></div>'
        rows = [home_row(s, swarm_state(s["slug"]), now) for s in ledger_summaries()]
        count = len(ledger_bin.entries())
        badge = f'<span class="count">{count}</span>' if count else ""
        fab = f'<a class="fab" id="bin-fab" href="/?view=bin" title="Bin" aria-label="Bin">{TRASH}{badge}</a>'
    head = "".join(head_cell(label) for label in HEADS[view])
    body = "".join(rows) or f'<li class="empty">{empty}</li>'
    values = {
        "HEADING": heading,
        "PALETTE": core.PALETTE.read_text(encoding="utf-8"),
        "WATERMARK": watermark,
        "VIEW": view,
        "TOTAL": total,
        "HEAD": head,
        "BODY": body,
        "FAB": fab,
        "TOOLTIPS": core.TOOLTIPS.read_text(encoding="utf-8"),
    }
    return re.sub(
        r"/\*__HOME_(TOOLTIPS)__\*/|__HOME_(HEADING|PALETTE|WATERMARK|VIEW|TOTAL|HEAD|BODY|FAB)__",
        lambda m: values[m.group(1) or m.group(2)],
        HOME_PAGE.read_text(encoding="utf-8"),
    )


def page_for(slug):
    """The page as served: when an agent broke the seed, the JSON's document stands in for it."""
    try:
        embedded = core.PAGE_RE.search(repository.read_page(slug))
        if not embedded or embedded.group(1) != core.page_version():
            new_ledger.upgrade_page(slug)
        state = repository.get_document(slug)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"sync {slug}: {exc}\n")
        return repository.read_page(slug)
    page = repository.read_page(slug)
    if state["_meta"].get("seed_error"):
        doc = {k: v for k, v in state.items() if k != "_meta"}
        page = core.SEED_RE.sub(
            lambda m: m.group(1) + core.seed_text(doc, state["_meta"]["rev"]) + m.group(3), page, count=1
        )
    return page


@functools.cache
def swarm_store():
    from scripts.swarm.store import connect

    return connect()


def swarm_status(slug, state=None):
    from scripts.swarm.status import status_report
    from scripts.swarm.store import SwarmError

    try:
        state = repository.read_snapshot(slug) if state is None else state
        return status_report(swarm_store(), slug, state)
    except SwarmError:
        return None
    except Exception as exc:  # the page keeps its last observed state; the log keeps why this read failed
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
        ("codex_share", "codex-share", 100),
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
    if "gates" in body:
        pairs += gate_pairs(body["gates"])
    if "master_agent" in body:
        if body["master_agent"] not in MASTER_AGENTS:
            raise ValueError(f"master_agent must be one of {', '.join(MASTER_AGENTS)}")
        pairs.append(f"master-agent={body['master_agent']}")
    if not pairs:
        raise ValueError(
            "set needs max_eng, max_ci, max_plan, codex_share, compact_limit, effort_min, effort_max, autonomy, "
            "master_agent or gates"
        )
    return ["set", *pairs]


def gate_pairs(gates):
    from scripts.gates import catalog, modes

    names = catalog.defaults()
    if (
        not isinstance(gates, dict)
        or not gates
        or not all(n in names and isinstance(m, str) and modes.normalize(m) in modes.MODES for n, m in gates.items())
    ):
        raise ValueError(f"gates maps a gate of {', '.join(names)} to {', '.join(modes.LABELS.values())}")
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
    status = swarm_status(slug)
    if not status or not any(agent["name"] == name for agent in status.get("agents", [])):
        return None, "agent is not in this swarm"
    exe = shutil.which("agentihooks")
    argv = [exe, "terminate-agent", name, "--type", "any"]
    for command in ([*argv, "--dry-run"], argv):
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if done.returncode:
            return None, (done.stderr or done.stdout).strip() or "agent termination failed"
    return swarm_status(slug), ""


def swarm_control(slug, argv, command="swarm"):
    exe = shutil.which("agentihooks")
    if not exe:
        return None, "agentihooks is not on PATH"
    try:
        if command == "swarm" and argv[0] == "terminate":
            return terminate_control(slug, argv[1])
        env = {**os.environ, "AGENTIHOOKS_AGENT_NAME": "operator", "AGENTIHOOKS_CONTROL_SOURCE": "page"}
        done = subprocess.run([exe, command, slug, *argv], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if done.returncode != 0:
        return None, (done.stderr or done.stdout).strip() or "swarm command failed"
    status = swarm_status(slug)
    return status, "" if status else "swarm status unreadable after the command"


def probe_quota() -> str:
    exe = shutil.which("agentihooks")
    if not exe:
        return "agentihooks is not on PATH"
    try:
        done = subprocess.run(
            [exe, "quota", "--refresh", "--json"], capture_output=True, text=True, timeout=QUOTA_PROBE_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return "" if done.returncode == 0 else (done.stderr or done.stdout).strip() or "quota probe failed"


def refresh_quota(slug: str) -> tuple[dict | None, str]:
    from scripts import agents_quota

    error = agents_quota.refresh_page_quota(probe_quota)
    if error:
        return None, error
    status = swarm_status(slug)
    return status, "" if status else "swarm status unreadable after the quota probe"


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
    """Alerts raised by this sync go to the master's seat or the operator through the inbox."""
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
        return ledger_alerts.deliver(inbox, slug, state["alerts"], meta["rev"], master)
    except Exception as exc:  # the ledger write stands whatever the inbox does
        sys.stderr.write(f"alert delivery for {slug}: {exc}\n")
        return []


def doctor_phrase(slug, state):
    """The operator's chat line rig doctor stop stops the Doctor of this ledger, in the background."""
    meta = state["_meta"]
    said = [
        e
        for e in meta.get("events", [])
        if e.get("rev") == meta["rev"] and e.get("by") == "operator" and e.get("target") == "chat"
    ]
    exe = shutil.which("agentihooks")
    if exe and any(e.get("text", "").strip().lower() == DOCTOR_PHRASE for e in said):
        subprocess.Popen([exe, "doctor", slug, "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def with_workspaces(slug, state):
    """The latest progress and proof lines of each task's work folder, read at reply time and never stored."""
    tasks = [
        {**t, "workspace_tail": ledger_workspace.tails(slug, t["id"])} if t.get("workspace") else t
        for t in state.get("tasks", [])
    ]
    return {**state, "tasks": tasks}


def tail_stamp(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def workspace_tails(slug, ledger):
    """Each task's work folder tails, read again only when one of its files changed."""
    marks, kept, found = TAIL_MARKS.get(slug, {}), {}, {}
    for task in ledger.get("tasks", []):
        if not task.get("workspace"):
            continue
        try:
            folder = ledger_workspace.folder(slug, task["id"])
        except ValueError:
            continue
        stamp = tuple(tail_stamp(folder / name) for _, name in ledger_workspace.TAILS)
        kept[task["id"]] = marks.get(task["id"])
        if kept[task["id"]] is None or kept[task["id"]][0] != stamp:
            kept[task["id"]] = (stamp, ledger_workspace.tails(slug, task["id"]))
        found[task["id"]] = kept[task["id"]][1]
    TAIL_MARKS[slug] = kept
    return found


def stream_resources(slug):
    ledger = ledger_view(repository.get_document(slug, reconcile=False))
    return {"ledger": ledger, "swarm": swarm_status(slug, ledger), "workspaces": workspace_tails(slug, ledger)}


def sample_streams():
    """Publish swarm status and work folder tails for every ledger a stream is open on."""
    HUB.evict()
    for slug in [slug for slug in TAIL_MARKS if not HUB.has(slug)]:
        del TAIL_MARKS[slug]
    for slug in HUB.watched():
        ledger = HUB.resource(slug, "ledger")
        if ledger is None:
            continue
        try:
            HUB.publish(slug, "swarm", swarm_status(slug, ledger))
            HUB.publish(slug, "workspaces", workspace_tails(slug, ledger))
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

    def slug(self):
        return self.path.split("?", 1)[0].strip("/").removeprefix("api/").removesuffix(".html")

    def exists(self, slug):
        return core.SLUG_RE.match(slug) and repository.exists(slug)

    def refused(self, slug=None):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self.send(403, "host not allowed", "text/plain") or True
        if slug is not None:
            self.principal = authority.principal(
                core.read_token(repository.read_page(slug)),
                slug,
                self.headers.get("X-Ledger-Token"),
                self.headers.get("X-Ledger-Agent"),
            )
            if self.principal is None:
                return self.send(403, "missing or wrong ledger token", "text/plain") or True
        return False

    def agent_view(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("view") == ["agent"]

    def reply_state(self, slug, changes=None, ops=None, refusals=None):
        refusals = refusals or {}
        try:
            state, rejected = repository.apply_ops(
                slug, changes=changes, ops=ops, gate=talk.Budget(slug) if ops else None
            )
        except (ValueError, OSError) as exc:
            return self.send(500, f"ledger unreadable: {exc}", "text/plain")
        if changes or ops:
            relay_to_inbox(slug, state)
            doctor_phrase(slug, state)
        deliver_alerts(slug, state)
        state["_meta"] = {
            **{k: v for k, v in state["_meta"].items() if k != "seeds"},
            "page_version": core.page_version(),
            "crew": ledger_gate.crew(state["_meta"]),
        }
        state["_meta"]["warnings"] = [*state["_meta"].get("warnings", []), *refusals.values()]
        reply = {
            **(state if self.agent_view() else with_workspaces(slug, state)),
            "rejected": [*rejected, *refusals],
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
        if route == "/logo.png":
            return self.send_logo()
        if route.startswith("/media/"):
            return self.send_media(*route.removeprefix("/media/").partition("/")[::2])
        if route.startswith("/artifacts/"):
            return self.send_media(*route.removeprefix("/artifacts/").partition("/")[::2], store=ledger_artifacts)
        if route.startswith("/static/"):
            return self.send_module(route)
        if route == "/":
            ledger_bin.tidy()
            bin_closed_without_swarm()
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
            return None if self.refused(slug) else self.reply_state(slug)
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

    def send_module(self, route):
        match = MODULE_RE.fullmatch(route)
        path = match and core.MODULES / f"{match.group(2)}.js"
        if not path or match.group(1) != core.page_version() or not path.is_file():
            return self.send(404, "no such module", "text/plain")
        return self.send(200, path.read_bytes().decode(), "text/javascript; charset=utf-8")

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
            doc = repository.get_document(slug, reconcile=False)
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
            meta = repository.get_document(slug, reconcile=False)["_meta"]
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
            state = repository.get_document(slug, reconcile=not core.paths(slug)[1].exists())
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
        return self.reply_state(slug, changes, allowed, refusals)


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


def watch_seeds(interval=2.0):
    seen = {}
    started = code_stamp()
    while True:
        reload_if_changed(started)
        try:
            ledger_bin.tidy()
        except OSError as exc:
            sys.stderr.write(f"bin purge: {exc}\n")
        for path in repository.pages():
            try:
                mtime = path.stat().st_mtime
                if seen.get(path) != mtime:
                    seen[path] = mtime
                    repository.get_document(path.stem)
            except Exception as exc:  # the loop must outlive any one bad ledger
                sys.stderr.write(f"skip {path.name}: {exc}\n")
        sample_streams()
        time.sleep(interval)


def serving_dir(timeout: float = 1):
    try:
        with urllib.request.urlopen(f"{BASE}/healthz", timeout=timeout) as resp:
            return json.loads(resp.read()).get("dir")
    except (OSError, ValueError):
        return None


def check_address() -> None:
    if PORT == 8765 and not ledger_link.shared_directory(core.LEDGER_DIR):
        sys.exit("port 8765 is reserved for the shared ledger folder; proof folders require a spare LEDGER_PORT")


def serve():
    check_address()
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_seeds, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    PIDFILE.write_text(str(os.getpid()))
    print(f"ledger server on {BASE}, dir {core.LEDGER_DIR}", flush=True)
    server.serve_forever()


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
    check_address()
    deadline = time.monotonic() + SERVER_WAIT
    started = False
    running = serving_dir()
    while not running:
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
                )
            started = True
        time.sleep(min(0.1, remaining))
        running = serving_dir(timeout=min(1, remaining))
    if running != str(core.LEDGER_DIR):
        sys.exit(f"{BASE} already serves {running}, not {core.LEDGER_DIR}; stop that ledger server first")
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
