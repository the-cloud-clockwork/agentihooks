import contextlib
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

MODULES = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "static" / "js"
SHELL = MODULES.parents[1] / "shell.html"
PAGE_URL = "http://127.0.0.1:9/ledger"
ORDER = (
    "config",
    "dom",
    "pages",
    "api",
    "patch",
    "freezes",
    "state",
    "sync",
    "markdown",
    "media",
    "artifacts",
    "threads",
    "render",
    "notices",
)
ORDER += ("outline", "folds", "layout", "chat", "swarm", "controls", "events", "main")


def serve_modules(target, ledger=None, swarm=None):
    """Answer every /static/<version>/ asset the shells load, and with a ledger the event stream that carries it."""
    if ledger is not None:
        target.route(lambda url: is_events(url), lambda route: fulfill_events(route, ledger, swarm))
    from scripts.swarm_ledger import ledger_core

    def answer(route):
        name = urlsplit(route.request.url).path.split("/", 3)[3]
        path = ledger_core.static_assets().get(name)
        if path is None:
            return route.fulfill(status=404, body="no such asset")
        ctype = "text/css" if path.suffix == ".css" else "text/javascript"
        return route.fulfill(body=path.read_text(encoding="utf-8"), content_type=f"{ctype}; charset=utf-8")

    target.route("**/static/*/**", answer)


def shell_html(title="Ledger"):
    """The ledger shell as served, with its placeholders left in except the title."""
    return SHELL.read_text(encoding="utf-8").replace("__LEDGER_TITLE__", title)


def ledger_state(doc, rev=1):
    return {**doc, "_meta": {"rev": rev, **doc.get("_meta", {})}}


def snapshot_body(ledger=None, swarm=None):
    data = {"ledger": ledger, "swarm": swarm}
    return f"id: c0\nevent: snapshot\ndata: {json.dumps(data)}\n\n"


def is_events(url):
    path = urlsplit(url).path
    return path.startswith("/api/v1/ledgers/") and path.endswith("/events")


def fulfill_events(route, ledger=None, swarm=None):
    """Answer the page's event stream with one snapshot; the page reconnects after the body ends."""
    route.fulfill(body=snapshot_body(ledger, swarm), content_type="text/event-stream")


def show(tab, html, url=PAGE_URL, ledger=None, swarm=None):
    tab.route(url, lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
    serve_modules(tab, ledger, swarm)
    tab.goto(url)
    if ledger is not None:
        loaded(tab)


def loaded(tab):
    """Wait until the page applied the stream's first ledger state."""
    tab.wait_for_function(
        """async () => {
          const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
          await (await import(new URL("sync.js", main))).loaded;
          return true;
        }"""
    )


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
    return page


@contextlib.contextmanager
def served(server):
    """A live ledger server thread on a spare port, answering for the module the test patched."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
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
