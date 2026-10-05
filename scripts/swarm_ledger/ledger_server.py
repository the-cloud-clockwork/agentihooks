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
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
import ledger_bin  # noqa: E402
import ledger_close  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate  # noqa: E402
import ledger_media  # noqa: E402
import ledger_size  # noqa: E402
import ledger_workspace  # noqa: E402
import new_ledger  # noqa: E402

HOST = os.environ.get("LEDGER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LEDGER_PORT", "8765"))
BASE = f"http://{HOST}:{PORT}"
PIDFILE = core.LEDGER_DIR / ".server.pid"
LOGFILE = core.LEDGER_DIR / ".server.log"
FILE_ORIGIN = "null"
MAX_BODY = 1 << 20
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
CODE_DIRS = (CODE_DIR, ROOT / "scripts" / "inbox", ROOT / "scripts" / "swarm")


def all_summaries():
    found = []
    for path in sorted(core.LEDGER_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
        json_path = core.paths(path.stem)[1]
        try:
            seed = core.parse_seed(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            seed = None
        try:
            doc, _, _ = core.load_state(json_path, seed)
        except (ValueError, OSError):
            continue
        found.append(
            {
                "slug": path.stem,
                "title": doc.get("title") or path.stem,
                "overview": ledger_close.intro(doc.get("overview") or ""),
                "closed_at": doc.get("closed_at"),
                "size": ledger_size.size_of(doc),
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
    "main{max-width:960px;margin:0 auto;padding:40px 16px 96px}section.closed{margin-top:24px}"
    "h1{display:flex;align-items:center;gap:10px;margin:0;padding:14px 18px;font-size:12px;font-weight:700;"
    "letter-spacing:.16em;text-transform:uppercase;color:var(--text);background:var(--surface-1);border-radius:6px 6px 0 0;"
    "border-bottom:1px solid var(--signal-soft)}"
    "h1::before{content:'';width:2px;height:14px;background:var(--signal);box-shadow:0 0 8px var(--signal)}"
    "ul{margin:0;padding:4px 18px 8px 36px;background:var(--surface-1);border-radius:0 0 6px 6px}"
    "li{padding:12px 0;border-top:1px solid var(--rule)}li::marker{color:var(--signal)}"
    "li:first-child{border-top:0}.row{display:flex;gap:12px;align-items:flex-start}.info{flex:1;min-width:0}"
    "a{color:var(--link);font-size:14px;font-weight:700;text-decoration:none}"
    "a:hover{text-decoration:underline}a:focus-visible{outline:1px solid var(--link);outline-offset:3px;border-radius:2px}"
    "p{margin:2px 0 0;color:var(--muted);overflow-wrap:anywhere}"
    ".meta{display:flex;gap:14px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}"
    ".left{color:var(--link);text-shadow:0 0 8px currentColor}"
    ".size{margin-left:10px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--signal);"
    "text-shadow:0 0 8px currentColor}"
    ".act{flex:none;width:32px;height:32px;display:grid;place-content:center;border:1px solid transparent;"
    "border-radius:8px;background:transparent;color:var(--muted);cursor:pointer;"
    "transition:background .15s,border-color .15s,box-shadow .15s,color .15s}"
    ".act svg{width:16px;height:16px}.act:disabled{opacity:.3;cursor:wait}"
    ".act.reopen{width:auto;padding:0 10px;color:var(--link)}"
    ".act:hover,.act:focus-visible{outline:none;background:var(--hover);border-color:var(--edge);"
    "box-shadow:0 0 12px -2px currentColor}"
    ".act:focus-visible{outline:1px solid currentColor;outline-offset:2px}"
    ".act.del:hover,.act.del:focus-visible{color:var(--destructive)}"
    ".act.restore:hover,.act.restore:focus-visible{color:var(--link)}"
    ".fab{position:fixed;left:16px;bottom:16px;z-index:10;width:44px;height:44px;border-radius:12px;display:grid;"
    "place-content:center;color:var(--signal);background:var(--surface-2);border:1px solid transparent;"
    "backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);"
    "transition:background .15s,border-color .15s,box-shadow .15s,color .15s}"
    ".fab svg{width:20px;height:20px}.fab:hover,.fab:focus-visible{background:var(--hover);"
    "border-color:var(--edge);box-shadow:0 0 14px -2px currentColor}"
    ".fab:focus-visible{outline:1px solid var(--signal);outline-offset:2px}"
    ".count{position:absolute;top:-6px;right:-4px;font-size:11px;font-weight:700;color:var(--destructive);"
    "text-shadow:0 0 6px currentColor}"
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


def ledger_row(s, control):
    slug, title = html.escape(s["slug"]), html.escape(s["title"])
    size = f'<span class="size">{html.escape(s["size"])}</span>'
    return (
        f'<li><div class="row"><div class="info"><a href="/{slug}">{title}</a>{size}'
        f"<p>{html.escape(s['overview'])}</p>{s.get('meta', '')}</div>{control.format(slug=slug, title=title)}</div></li>"
    )


def bin_meta(s):
    deleted = time.strftime("%Y-%m-%d", time.localtime(s["deleted_at"] / 1000))
    days = s["days_left"]
    return f'<p class="meta"><span>Deleted {deleted}</span><span class="left">{days} day{"" if days == 1 else "s"} left</span></p>'


def closed_meta(s):
    return f'<p class="meta"><span>Closed {time.strftime("%Y-%m-%d", time.localtime(s["closed_at"] / 1000))}</span></p>'


def index_page(view="home"):
    closed = ""
    if view == "bin":
        heading, empty = "BIN", "The bin is empty."
        control = (
            '<button class="act restore" type="button" data-act="restore" data-slug="{slug}" '
            f'title="Restore to HOME" aria-label="Restore {{title}} to HOME">{RESTORE}</button>'
        )
        rows = [ledger_row({**s, "meta": bin_meta(s)}, control) for s in bin_summaries()]
        fab = f'<a class="fab" id="home-fab" href="/" title="HOME" aria-label="HOME">{HOME_ICON}</a>'
    else:
        heading, empty = "HOME", "No ledgers yet."
        control = (
            '<button class="act del" type="button" data-act="delete" data-slug="{slug}" '
            f'title="Move to the bin" aria-label="Move {{title}} to the bin">{TRASH}</button>'
        )
        summaries = ledger_summaries()
        rows = [ledger_row(s, control) for s in summaries if not s["closed_at"]]
        reopen = (
            '<button class="act reopen" type="button" data-act="reopen" data-slug="{slug}" '
            'aria-label="Reopen {title}">Reopen</button>'
        )
        ended = [ledger_row({**s, "meta": closed_meta(s)}, reopen + control) for s in summaries if s["closed_at"]]
        if ended:
            closed = f'<section class="closed"><h1>CLOSED</h1><ul>{"".join(ended)}</ul></section>'
        count = len(ledger_bin.entries())
        badge = f'<span class="count">{count}</span>' if count else ""
        fab = f'<a class="fab" id="bin-fab" href="/?view=bin" title="Bin" aria-label="Bin">{TRASH}{badge}</a>'
    body = "\n".join(rows) or f"<li>{empty}</li>"
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{heading}</title><style>{core.PALETTE.read_text(encoding='utf-8')}{HOME_STYLE}</style>"
        f"<main><h1>{heading}</h1><ul>{body}</ul>{closed}</main>{fab}{BIN_SCRIPT}"
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


def swarm_status(slug):
    exe = shutil.which("agentihooks")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "swarm", slug, "status", "--json"], capture_output=True, text=True, timeout=20)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
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
MAX_NOTE = 500
FINDING_RE = re.compile(r"^[a-z][a-z-]*/[\w.-]{1,64}$")


def control_argv(body):
    action = body.get("action") if isinstance(body, dict) else None
    if action in CONTROLS:
        return CONTROLS[action]
    if action == "verdict":
        return verdict_argv(body)
    if action != "set":
        raise ValueError("action must be start, pause, stop, stop_now, close, reopen, set or verdict")
    pairs = []
    for key, flag, limit in (
        ("max_eng", "max-eng-agents", MAX_CAP),
        ("max_ci", "max-ci-agents", MAX_CAP),
        ("codex_share", "codex-share", 100),
    ):
        value = body.get(key)
        if value is None:
            continue
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError(f"{key} must be a whole number from 0 to {limit}")
        pairs.append(f"{flag}={value}")
    if not pairs:
        raise ValueError("set needs max_eng, max_ci or codex_share")
    return ["set", *pairs]


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


def swarm_control(slug, argv, command="swarm"):
    exe = shutil.which("agentihooks")
    if not exe:
        return None, "agentihooks is not on PATH"
    try:
        done = subprocess.run([exe, command, slug, *argv], capture_output=True, text=True, timeout=60)
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

    def reply_state(self, slug, changes=None, ops=None):
        try:
            state, rejected = core.sync(slug, changes=changes, ops=ops)
        except (ValueError, OSError) as exc:
            return self.send(500, f"ledger unreadable: {exc}", "text/plain")
        if changes or ops:
            relay_to_inbox(slug, state)
            doctor_phrase(slug, state)
        state["_meta"] = {
            **state["_meta"],
            "page_version": core.page_version(),
            "crew": ledger_gate.crew(state["_meta"]),
        }
        reply = {**with_workspaces(slug, state), "rejected": rejected}
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

    def send_media(self, slug, media_id):
        try:
            if not core.SLUG_RE.match(slug):
                raise ValueError("not a ledger")
            data = ledger_media.path_of(slug, media_id).read_bytes()
        except (ValueError, OSError):
            return self.send(404, "no such image", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", ledger_media.TYPES[media_id.rsplit(".", 1)[1]])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def post_media(self, slug):
        if not self.exists(slug):
            return self.send(404, "no such ledger", "text/plain")
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            return self.send(403, "origin not allowed", "text/plain")
        if self.refused(slug):
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > ledger_media.MAX_BYTES:
            self.close_connection = True
            return self.send(413, f"an image may be at most {ledger_media.MAX_BYTES >> 20} MB", "text/plain")
        if length <= 0:
            return self.send(400, "the upload is empty", "text/plain")
        try:
            attachment = ledger_media.store(slug, self.rfile.read(length))
        except ledger_media.Refused as exc:
            return self.send(exc.status, str(exc), "text/plain")
        return self.send(200, json.dumps(attachment), "application/json")

    def do_POST(self):
        if self.refused():
            return None
        route = self.path.split("?", 1)[0]
        if route.startswith("/api/media/"):
            return self.post_media(route.removeprefix("/api/media/"))
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
        if action == "delete":
            ledger_bin.delete(slug)
        elif not ledger_bin.restore(slug):
            return self.send(404, "not in the bin", "text/plain")
        return self.send(200, json.dumps({"binned": sorted(ledger_bin.entries())}), "application/json")

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


def serving_dir():
    try:
        with urllib.request.urlopen(f"{BASE}/healthz", timeout=1) as resp:
            return json.loads(resp.read()).get("dir")
    except (OSError, ValueError):
        return None


def serve():
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_seeds, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    PIDFILE.write_text(str(os.getpid()))
    print(f"ledger server on {BASE}, dir {core.LEDGER_DIR}", flush=True)
    server.serve_forever()


def ensure():
    running = serving_dir()
    if running and running != str(core.LEDGER_DIR):
        sys.exit(f"{BASE} already serves {running}, not {core.LEDGER_DIR}; set LEDGER_PORT to another port")
    if not running:
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
        for _ in range(50):
            if serving_dir():
                break
            time.sleep(0.1)
        else:
            sys.exit(f"ledger server did not answer on {BASE}; see {LOGFILE}")
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
