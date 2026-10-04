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
import ledger_core as core  # noqa: E402
import ledger_gate  # noqa: E402
import new_ledger  # noqa: E402

HOST = os.environ.get("LEDGER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LEDGER_PORT", "8765"))
BASE = f"http://{HOST}:{PORT}"
PIDFILE = core.LEDGER_DIR / ".server.pid"
LOGFILE = core.LEDGER_DIR / ".server.log"
FILE_ORIGIN = "null"
MAX_BODY = 1 << 20
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def index_page():
    rows = []
    for path in sorted(core.LEDGER_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
        slug = html.escape(path.stem)
        rows.append(f'<li><a href="/{slug}">{slug}</a></li>')
    body = "\n".join(rows) or "<li>No ledgers yet.</li>"
    return (
        "<!doctype html><meta charset=utf-8><title>Ledgers</title>"
        "<style>body{background:#04060d;color:#f8fafc;font:14px/1.6 system-ui,sans-serif;padding:32px}"
        "a{color:#60a5fa}h1{font-size:22px;border-bottom:1px solid #ef4444;padding-bottom:8px}</style>"
        f"<h1>Development ledgers</h1><ul>{body}</ul>"
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
        state["_meta"] = {
            **state["_meta"],
            "page_version": core.page_version(),
            "crew": ledger_gate.crew(state["_meta"]),
        }
        return self.send(200, json.dumps({**state, "rejected": rejected}, ensure_ascii=False), "application/json")

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
        if route == "/":
            return self.send(200, index_page(), "text/html; charset=utf-8")
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

    def do_PUT(self):
        slug = self.slug()
        if self.refused():
            return None
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
        except ValueError as exc:
            return self.send(400, f'body must be {{"changes": [...], "ops": [...]}}: {exc}', "text/plain")
        return self.reply_state(slug, changes, ops)


def watch_seeds(interval=2.0):
    seen = {}
    while True:
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
