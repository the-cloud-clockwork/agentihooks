import contextlib
import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

MODULES = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "static" / "js"
SHELL = MODULES.parents[1] / "shell.html"
PAGE_URL = "http://127.0.0.1:9/ledger"
NAVIGATION_MS = 15000
ORDER = (
    "config",
    "dom",
    "pages",
    "api",
    "patch",
    "freezes",
    "frozen_view",
    "state",
    "sync",
    "phase_review",
    "markdown",
    "media",
    "artifacts",
    "threads",
    "render_cycle",
    "render",
    "stats",
    "edits",
    "notices",
)
ORDER += (
    "outline",
    "outline_keep",
    "walk",
    "folds",
    "layout",
    "chat",
    "lifecycle",
    "roles",
    "agent_cells",
    "finding_rows",
    "swarm",
    "recovery",
    "context",
    "hives",
    "architecture",
    "status_note",
    "controls",
    "events",
    "main",
)


def serve_modules(target: Any, ledger: dict | None = None, swarm: dict | None = None) -> None:
    """Answer every /static/<version>/ asset the shells load, and with a ledger the event stream that carries it."""
    if ledger is not None:
        target.route(lambda url: is_events(url), lambda route: fulfill_events(route, ledger, swarm))
    from scripts.swarm_ledger import ledger_core

    ledger_core.static_assets()

    def answer(route):
        name = urlsplit(route.request.url).path.split("/", 3)[3]
        path = ledger_core.static_assets().get(name)
        if path is None:
            return route.fulfill(status=404, body="no such asset")
        ctype = "text/css" if path.suffix == ".css" else "text/javascript"
        return route.fulfill(body=path.read_text(encoding="utf-8"), content_type=f"{ctype}; charset=utf-8")

    from scripts.swarm_ledger import page_tips

    tips = page_tips.values()
    target.route("**/static/*/**", answer)
    target.route("**/api/v1/tips", lambda route: route.fulfill(json={"data": tips}))


def shell_html(title="Ledger"):
    """The ledger shell as served, with its placeholders left in except the title."""
    return SHELL.read_text(encoding="utf-8").replace("__LEDGER_TITLE__", title)


def ledger_state(doc, rev=1):
    return {**doc, "_meta": {"rev": rev, **doc.get("_meta", {})}}


def open_body(ledger: dict | None = None, swarm: dict | None = None) -> str:
    view = served_ledger(ledger)
    rev = view.get("_meta", {}).get("rev") if isinstance(view, dict) else None
    return f"id: c0\nevent: open\ndata: {json.dumps({'rev': rev, 'swarm': swarm})}\n\n"


def summary_of(view: dict) -> dict:
    """The summary read of a served page view: every part but the paged sections, with their open counts."""
    from scripts.swarm_ledger.api import summary

    data = {key: value for key, value in view.items() if key not in summary.PAGED}
    data["sections"] = {name: summary.section(name, view.get(name) or []) for name in summary.PAGED}
    return data


def view_page(view: dict, name: str) -> dict:
    """Every row a served page view holds for one section, in stored order, as one open read page."""
    rows = [{**row, "position": at} for at, row in enumerate(view.get(name) or []) if isinstance(row, dict)]
    return {"data": rows, "revision": "r", "total": len(rows), "next_offset": None}


def served_ledger(ledger):
    if not isinstance(ledger, dict):
        return ledger
    from scripts.swarm import phase_state
    from scripts.swarm_ledger import ledger_server

    meta = {"limits": ledger_server.PAGE_LIMITS, **ledger.get("_meta", {})}
    closed = ledger.get("_meta", {}).get("closed", {})
    hidden = [{"id": pid, "done": True} for pid in closed.get("phases", {}).get("ids", [])]
    plans = [{"phase": pid, "kind": "plan", "state": "done"} for pid in closed.get("tasks", {}).get("plan_phases", [])]
    full = {**ledger, "phases": [*ledger.get("phases", []), *hidden], "tasks": [*ledger.get("tasks", []), *plans]}
    status = {key: item for key, item in phase_state.status(full).items() if key not in {p["id"] for p in hidden}}
    return {"marks": {"frozen": [], "held": []}, "phase_status": status, **ledger, "_meta": meta}


def is_events(url: str) -> bool:
    """Whether url is a ledger feed request of the page: the event stream, the summary or an open section read."""
    parts = urlsplit(url)
    if not parts.path.startswith("/api/v1/ledgers/"):
        return False
    return parts.path.endswith(("/events", "/summary")) or "state=open" in parts.query


def fulfill_events(route: Any, ledger: dict | None = None, swarm: dict | None = None) -> None:
    """Answer the event stream with one open frame, after which the page reconnects, and the summary and open
    section reads from the served ledger."""
    parts = urlsplit(route.request.url)
    if parts.path.endswith("/events"):
        return route.fulfill(body=open_body(ledger, swarm), content_type="text/event-stream")
    if ledger is None:
        return route.fulfill(status=404, body="no ledger")
    view = served_ledger(ledger)
    if parts.path.endswith("/summary"):
        return route.fulfill(json={"data": summary_of(view), "revision": "r"})
    return route.fulfill(json=view_page(view, parts.path.rsplit("/", 1)[1]))


def show(tab, html, url=PAGE_URL, ledger=None, swarm=None):
    tab.route(url, lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
    serve_modules(tab, ledger, swarm)
    tab.goto(url, timeout=NAVIGATION_MS)
    if ledger is not None:
        loaded(tab)


def loaded(tab):
    """Wait until the page applied the stream's first ledger state."""
    tab.wait_for_function(
        """() => {
          if (!window.__ledgerTestLoaded) {
            window.__ledgerTestLoaded = {ready: false};
            const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
            import(new URL("sync.js", main)).then(sync => sync.loaded)
              .then(() => { window.__ledgerTestLoaded.ready = true; });
          }
          return window.__ledgerTestLoaded.ready;
        }"""
    )


def reload_after_folds(tab, selector):
    state = tab.locator(selector).evaluate_all(
        """boxes => boxes.map(box => ({
          id: box.id,
          key: box.dataset.key || null,
          open: box.open
        }))"""
    )
    assert state
    keys = tab.evaluate(
        """async () => {
          const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
          const {FOLD_KEY, COMMENTS_KEY} = await import(new URL("config.js", main));
          return {FOLD_KEY, COMMENTS_KEY};
        }"""
    )
    tab.wait_for_function(
        """({boxes, keys}) => {
          const folds = JSON.parse(localStorage.getItem(keys.FOLD_KEY) || "{}");
          const comments = JSON.parse(localStorage.getItem(keys.COMMENTS_KEY) || "{}");
          return boxes.every(box => box.key
            ? (comments[box.open ? "opened" : "closed"] || []).includes(box.key)
            : folds[box.id] === box.open);
        }""",
        arg={"boxes": state, "keys": keys},
    )
    tab.reload(timeout=NAVIGATION_MS)
    loaded(tab)


def worker_modules(name):
    source = (MODULES / name).read_text(encoding="utf-8")
    return re.findall(r"new\s+Worker\(\s*new\s+URL\(\s*[\"'](?:\./)?(\w+\.js)[\"']", source)


def page_source():
    """The ledger page as one text for text checks: shell markup, stylesheets and every module inlined."""
    from scripts.swarm_ledger import ledger_core

    body = []
    for name in ORDER:
        for line in (MODULES / f"{name}.js").read_text(encoding="utf-8").splitlines():
            if not line.startswith("import "):
                body.append(("  " + line.removeprefix("export ")) if line else "")
    script = "<script>\n(() => {\n" + "\n".join(body).strip("\n") + "\n})();\n</script>\n"
    page = shell_html("__LEDGER_TITLE__").replace(
        '<script type="module" src="/static/__LEDGER_PAGE__/js/main.js"></script>\n', script
    )
    for name, path in ledger_core.static_assets().items():
        text = path.read_text(encoding="utf-8")
        page = page.replace(
            f'<link rel="stylesheet" href="/static/__LEDGER_PAGE__/{name}">', f"<style>\n{text}</style>"
        )
        page = page.replace(f'<script src="/static/__LEDGER_PAGE__/{name}"></script>', f"<script>{text}</script>")
    workers = {worker for name in ORDER for worker in worker_modules(f"{name}.js")}
    for worker in sorted(workers):
        source = (MODULES / worker).read_text(encoding="utf-8")
        page += f'<script type="text/worker">{source}</script>\n'
    return page


class JoinedServer(ThreadingHTTPServer):
    # Non daemon request threads are joined by server_close, so none outlives its test and imports a module mid flight.
    daemon_threads = False


@contextlib.contextmanager
def served(server):
    """A live ledger server thread on a spare port, answering for the module the test patched."""
    httpd = JoinedServer(("127.0.0.1", 0), server.Handler)
    host = f"127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    with patch.object(server, "ALLOWED_HOSTS", {host}), patch.object(server, "ALLOWED_ORIGINS", {f"http://{host}"}):
        thread.start()
        try:
            yield f"http://{host}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


def rendered_home(server, browser, view="home", now=None):
    """HOME or BIN as the browser renders it from the live server, with the clock pinned at now."""
    with served(server) as base:
        page = browser.new_page()
        if now is not None:
            page.add_init_script(f"Date.now = () => {now};")
        page.goto(base + ("/?view=bin" if view == "bin" else "/"))
        page.wait_for_function("() => document.getElementById('rows').children.length > 0")
        html = page.content()
        page.close()
    return html


@contextlib.contextmanager
def chromium():
    import pytest

    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        yield browser
        browser.close()


def browser_home(server, view="home", now=None):
    with chromium() as browser:
        return rendered_home(server, browser, view, now)
