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
import ledger_artifacts  # noqa: E402
import ledger_bin  # noqa: E402
import ledger_close  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate  # noqa: E402
import ledger_link  # noqa: E402
import ledger_media  # noqa: E402
import ledger_size  # noqa: E402
import ledger_workspace  # noqa: E402
import new_ledger  # noqa: E402

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
CODE_DIRS = (CODE_DIR, *(ROOT / "scripts" / name for name in ("inbox", "swarm", "handoff", "doctor")))


def all_summaries():
    found = []
    for path in sorted(core.LEDGER_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
        json_path = core.paths(path.stem)[1]
        try:
            seed = core.parse_seed(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            seed = None
        try:
            doc, meta, _ = core.load_state(json_path, seed)
        except (ValueError, OSError):
            continue
        items = [i for i in doc.get("tasks") or doc.get("phases") or [] if not i.get("out_of_scope")]
        done = sum(1 for i in items if i.get("done") is True)
        found.append(
            {
                "slug": path.stem,
                "title": doc.get("title") or path.stem,
                "overview": ledger_close.intro(doc.get("overview") or ""),
                "closed_at": doc.get("closed_at"),
                "size": ledger_size.size_of(doc),
                "open": len(items) - done,
                "done": done,
                "updated_at": meta.get("updated_at"),
            }
        )
    return found


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


HOME_STYLE = (
    "*{box-sizing:border-box}html{color-scheme:dark}body{margin:0;min-height:100vh;color:var(--text);"
    "font:13px/1.6 ui-monospace,'JetBrains Mono',SFMono-Regular,Menlo,Consolas,monospace;background:var(--canvas);"
    "background-image:var(--backdrop);background-attachment:fixed}"
    "::selection{background:var(--selection);color:var(--text)}"
    "main{padding:32px clamp(16px,3vw,48px) 96px}"
    "header{display:flex;align-items:baseline;gap:16px;padding:0 12px 12px;border-bottom:1px solid var(--signal-soft)}"
    "h1{display:flex;align-items:center;gap:10px;margin:0;font-size:12px;font-weight:700;letter-spacing:.16em;"
    "text-transform:uppercase;color:var(--text)}"
    "h1::before{content:'';width:2px;height:14px;background:var(--signal);box-shadow:0 0 8px var(--signal)}"
    ".total{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}"
    "ul{list-style:none;margin:0;padding:0}"
    ".row{display:grid;grid-template-columns:minmax(160px,280px) 56px minmax(0,1fr) 64px 72px 112px 80px 104px;"
    "align-items:center;gap:20px;min-height:40px;padding:6px 12px;border-bottom:1px solid var(--rule);"
    "white-space:nowrap;font-variant-numeric:tabular-nums;transition:background .15s}"
    ".bin .row{grid-template-columns:minmax(160px,280px) 56px minmax(0,1fr) 112px 104px 104px}"
    "li.row:hover,li.row:focus-within{background:var(--hover)}"
    ".head{min-height:0;padding-top:14px;padding-bottom:8px;font-size:10px;letter-spacing:.14em;"
    "text-transform:uppercase;color:var(--dim)}"
    ".title{overflow:hidden;text-overflow:ellipsis;color:var(--link);font-size:14px;font-weight:700;text-decoration:none}"
    ".title:hover{text-decoration:underline;text-underline-offset:3px}"
    ".title:focus-visible{outline:1px solid var(--link);outline-offset:3px;border-radius:2px}"
    ".kind{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-2);"
    "text-shadow:0 0 6px currentColor}"
    ".ov{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}"
    ".num{text-align:right;color:var(--dim)}.num b{font-weight:700}"
    ".num.open b{color:var(--link);text-shadow:0 0 8px currentColor}"
    ".num.done b{color:var(--positive);text-shadow:0 0 8px currentColor}"
    ".state{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase}"
    ".state::before{content:'';flex:none;width:6px;height:6px;border-radius:50%;background:currentColor}"
    ".s-running{color:var(--positive);text-shadow:0 0 8px currentColor}"
    ".s-drained{color:var(--accent-2);text-shadow:0 0 8px currentColor}"
    ".s-paused,.s-stopping{color:var(--warn);text-shadow:0 0 8px currentColor}"
    ".s-closed{color:var(--signal);text-shadow:0 0 8px currentColor}"
    ".s-stopped,.s-none{color:var(--dim)}"
    ".when,.deleted{text-align:right;color:var(--dim)}.head .r{text-align:right}"
    ".left{text-align:right;color:var(--link);text-shadow:0 0 8px currentColor}"
    ".acts{display:flex;justify-content:flex-end;gap:4px}"
    ".act{flex:none;height:30px;min-width:30px;display:inline-flex;align-items:center;justify-content:center;gap:6px;"
    "padding:0 7px;border:0;border-radius:8px;background:transparent;color:var(--muted);font:inherit;font-size:12px;"
    "cursor:pointer;transition:background .15s,box-shadow .15s,color .15s,filter .15s}"
    ".act svg{width:15px;height:15px}"
    ".act:hover,.act:focus-visible{outline:none;background:var(--hover);box-shadow:inset 0 0 0 1px var(--edge);"
    "filter:drop-shadow(0 0 6px currentColor)}"
    ".act:focus-visible{outline:1px solid currentColor;outline-offset:2px}"
    ".act:disabled{opacity:.3;cursor:wait;filter:none}"
    ".act.del:hover,.act.del:focus-visible{color:var(--destructive)}"
    ".act.reopen,.act.restore{color:var(--link)}"
    ".empty{padding:28px 12px;color:var(--muted)}"
    ".fab{position:fixed;left:16px;bottom:16px;z-index:10;width:44px;height:44px;border:0;border-radius:12px;"
    "display:grid;place-content:center;color:var(--signal);background:transparent;"
    "transition:background .15s,filter .15s}"
    ".fab svg{width:20px;height:20px}"
    ".fab:hover,.fab:focus-visible{background:var(--hover);filter:drop-shadow(0 0 6px currentColor);outline:none}"
    ".fab:focus-visible{outline:1px solid var(--signal);outline-offset:2px}"
    ".count{position:absolute;top:-6px;right:-4px;font-size:11px;font-weight:700;color:var(--destructive);"
    "text-shadow:0 0 6px currentColor}"
    "@media (max-width:760px){.row,.bin .row{grid-template-columns:minmax(0,1fr) auto auto;gap:12px}"
    ".head,.kind,.ov,.num,.when,.deleted{display:none}}"
)
ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">{}</svg>'
)
TRASH = ICON.format(
    '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/><path d="M10 11v6M14 11v6"/>'
)
RESTORE = ICON.format('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>')
HOME_ICON = ICON.format('<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>')
BIN_SCRIPT = (
    "<script>async function reopenLedger(b){"
    "const slug=encodeURIComponent(b.dataset.slug),page=await fetch('/'+slug);if(!page.ok)return page;"
    "const doc=new DOMParser().parseFromString(await page.text(),'text/html');"
    "const token=doc.querySelector('meta[name=ledger-token]').content;"
    "return fetch('/api/swarm/'+slug,{method:'PUT',headers:{'Content-Type':'application/json',"
    "'X-Ledger-Token':token},body:JSON.stringify({action:'reopen'})});}"
    "document.addEventListener('click',async e=>{const b=e.target.closest('button[data-act]');if(!b)return;"
    "b.disabled=true;const r=b.dataset.act==='reopen'?await reopenLedger(b):"
    "await fetch('/api/bin',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({action:b.dataset.act,slug:b.dataset.slug})});if(r.ok)return location.reload();"
    "b.disabled=false;alert(await r.text())})</script>"
)


def ledger_row(s, cells, control):
    slug, title, overview = html.escape(s["slug"]), html.escape(s["title"]), html.escape(s["overview"])
    return (
        f'<li class="row"><a class="title" href="/{slug}" title="{title}">{title}</a>'
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


def bin_cells(s):
    deleted = time.strftime("%Y-%m-%d", time.localtime(s["deleted_at"] / 1000))
    days = s["days_left"]
    return f'<span class="deleted">{deleted}</span><span class="left">{days} day{"" if days == 1 else "s"} left</span>'


HEADS = {
    "home": ("Ledger", "Kind", "Overview", ">Open", ">Done", "Swarm", ">Activity", ""),
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


def index_page(view="home", now=None):
    now = core.now_ms() if now is None else now
    if view == "bin":
        heading, empty = "BIN", "The bin is empty."
        rows = [ledger_row(s, bin_cells(s), RESTORE_BUTTON) for s in bin_summaries(now)]
        fab = f'<a class="fab" id="home-fab" href="/" title="HOME" aria-label="HOME">{HOME_ICON}</a>'
    else:
        heading, empty = "HOME", "No ledgers yet."
        rows = [
            ledger_row(s, home_cells(s, swarm_state(s["slug"]), now), (REOPEN if s["closed_at"] else "") + DELETE)
            for s in ledger_summaries()
        ]
        count = len(ledger_bin.entries())
        badge = f'<span class="count">{count}</span>' if count else ""
        fab = f'<a class="fab" id="bin-fab" href="/?view=bin" title="Bin" aria-label="Bin">{TRASH}{badge}</a>'
    total = f'<span class="total">{len(rows)} ledger{"" if len(rows) == 1 else "s"}</span>'
    head = "".join(
        f'<span class="r">{label[1:]}</span>' if label.startswith(">") else f"<span>{label}</span>"
        for label in HEADS[view]
    )
    body = "".join(rows) or f'<li class="empty">{empty}</li>'
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{heading}</title><style>{core.PALETTE.read_text(encoding='utf-8')}{HOME_STYLE}</style>"
        f'<main class="{view}"><header><h1>{heading}</h1>{total}</header>'
        f'<div class="row head" aria-hidden="true">{head}</div><ul>{body}</ul></main>{fab}{BIN_SCRIPT}'
    )


def page_for(slug):
    """The page as served: when an agent broke the seed, the JSON's document stands in for it."""
    html_path = core.paths(slug)[0]
    try:
        embedded = core.PAGE_RE.search(html_path.read_text(encoding="utf-8"))
        if not embedded or embedded.group(1) != core.page_version():
            new_ledger.upgrade_page(slug)
        state, _ = core.sync(slug)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"sync {slug}: {exc}\n")
        return html_path.read_text(encoding="utf-8")
    page = html_path.read_text(encoding="utf-8")
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


def swarm_status(slug):
    from scripts.swarm.status import status_report
    from scripts.swarm.store import SwarmError

    try:
        state = core.loads(core.paths(slug)[1].read_text(encoding="utf-8"))
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
AUTONOMY = ("manual", "assist", "delegate", "full")
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
    if "autonomy" in body:
        if body["autonomy"] not in AUTONOMY:
            raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY)}")
        pairs.append(f"autonomy={body['autonomy']}")
    if not pairs:
        raise ValueError("set needs max_eng, max_ci, max_plan, codex_share, compact_limit or autonomy")
    return ["set", *pairs]


def restore_decision_argv(body):
    agent, choice = body.get("agent"), body.get("choice")
    if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9@_.-]{0,127}", agent):
        raise ValueError("A restore decision needs an agent name")
    if choice not in {"resume", "fresh"}:
        raise ValueError("Choose resume or fresh")
    return ["restore-decision", agent, choice]


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send(self, code, body, ctype):
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
        return core.SLUG_RE.match(slug) and core.paths(slug)[0].exists()

    def refused(self, slug=None):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self.send(403, "host not allowed", "text/plain") or True
        if slug is not None:
            token = core.read_token(core.paths(slug)[0].read_text(encoding="utf-8"))
            if not token or self.headers.get("X-Ledger-Token") != token:
                return self.send(403, "missing or wrong ledger token", "text/plain") or True
        return False

    def agent_view(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("view") == ["agent"]

    def reply_state(self, slug, changes=None, ops=None):
        try:
            state, rejected = core.sync(slug, changes=changes, ops=ops)
        except (ValueError, OSError) as exc:
            return self.send(500, f"ledger unreadable: {exc}", "text/plain")
        if changes or ops:
            relay_to_inbox(slug, state)
            doctor_phrase(slug, state)
        state["_meta"] = {
            **{k: v for k, v in state["_meta"].items() if k != "seeds"},
            "page_version": core.page_version(),
            "crew": ledger_gate.crew(state["_meta"]),
        }
        reply = {**(state if self.agent_view() else with_workspaces(slug, state)), "rejected": rejected}
        return self.send(200, json.dumps(reply, ensure_ascii=False), "application/json")

    def do_OPTIONS(self):
        self.send_response(204)
        if self.headers.get("Origin") == FILE_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", FILE_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, PUT")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ledger-Token")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        route, slug = self.path.split("?", 1)[0], self.slug()
        if self.refused():
            return None
        if route == "/healthz":
            return self.send(200, json.dumps({"dir": str(core.LEDGER_DIR)}), "application/json")
        if route.startswith("/media/"):
            return self.send_media(*route.removeprefix("/media/").partition("/")[::2])
        if route.startswith("/artifacts/"):
            return self.send_media(*route.removeprefix("/artifacts/").partition("/")[::2], store=ledger_artifacts)
        if route == "/":
            ledger_bin.tidy()
            view = "bin" if "view=bin" in self.path.partition("?")[2].split("&") else "home"
            return self.send(200, index_page(view), "text/html; charset=utf-8")
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body size out of range")
            body = core.loads(self.rfile.read(length) or b"{}")
            action = body.get("action") if isinstance(body, dict) else None
            command, argv = ("doctor", DOCTOR[action]) if action in DOCTOR else ("swarm", control_argv(body))
        except ValueError as exc:
            return self.send(400, str(exc), "text/plain")
        status, error = swarm_control(slug, argv, command)
        if error:
            return self.send(502, error, "text/plain")
        return self.send(200, json.dumps(status), "application/json")

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
        return self.receive(
            slug, ledger_artifacts.MAX_BYTES, lambda data: ledger_artifacts.store(slug, name, data), agents_only=True
        )

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
            with core.LOCK:
                _, meta, _ = core.load_state(core.paths(slug)[1], None)
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
        slug = self.slug()
        if self.refused():
            return None
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
            changes, ops = core.check_body(core.loads(self.rfile.read(length) or b"{}"))
            ledger_media.resolve(slug, ops)
            ledger_artifacts.resolve(slug, ops)
        except ValueError as exc:
            return self.send(400, f'body must be {{"changes": [...], "ops": [...]}}: {exc}', "text/plain")
        return self.reply_state(slug, changes, ops)


def code_stamp(code_dirs=CODE_DIRS):
    return max(
        (p.stat().st_mtime_ns for d in code_dirs for p in d.rglob("*") if p.suffix in (".py", ".html")), default=0
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
        for path in core.LEDGER_DIR.glob("*.html"):
            try:
                mtime = path.stat().st_mtime
                if seen.get(path) != mtime:
                    seen[path] = mtime
                    core.sync(path.stem)
            except Exception as exc:  # the loop must outlive any one bad ledger
                sys.stderr.write(f"skip {path.name}: {exc}\n")
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
