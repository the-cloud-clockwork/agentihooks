import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import MODULES, is_events, serve_modules, shell_html, snapshot_body
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.swarm_ledger.test_patch import CASES
from tests.swarm_ledger.test_tabs import DOC, SWARM, URL

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger.events import Hub, patch, stream  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
SLUG = "pageevents-2026-01-01"


def test_the_page_applies_every_patch_the_server_builds_to_the_same_value(browser):
    page = browser.new_page()
    source = (MODULES / "patch.js").read_text(encoding="utf-8").replace("export function", "function")
    cases = [[old, new, patch.diff(old, new)] for old, new in CASES if not patch.same(old, new)]
    results = page.evaluate(
        "([source, cases]) => { const applyPatch = new Function(source + '; return applyPatch;')();"
        " return cases.map(([old, , change]) => applyPatch(old, change)); }",
        [source, cases],
    )
    page.close()
    assert results == [new for _, new, _ in cases]


def stub_page(browser, answers):
    """Open the page with events answered from a list: a status code, or a (ledger, swarm) snapshot."""
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    html = shell_html()
    seen = []

    def route(request):
        url = request.request.url
        if is_events(url):
            seen.append({"url": url, "headers": request.request.headers})
            answer = answers[min(sum(is_events(r["url"]) for r in seen), len(answers)) - 1]
            if isinstance(answer, int):
                return request.fulfill(status=answer, body="{}", content_type="application/json")
            ledger, swarm, cursor = answer
            body = snapshot_body(ledger, swarm).replace("id: c0", f"id: {cursor}")
            return request.fulfill(body=body, content_type="text/event-stream")
        if url.startswith(URL):
            return request.fulfill(body=html, content_type="text/html")
        seen.append({"url": url, "headers": request.request.headers})
        return request.abort()

    context.route("**/*", route)
    serve_modules(context)
    page = context.new_page()
    page.goto(URL)
    return context, page, seen


def test_the_page_sends_its_cursor_in_a_header_and_reloads_without_it_after_410(browser):
    doc = {**DOC, "_meta": {"rev": 3}}
    context, page, seen = stub_page(browser, [(doc, SWARM, "first"), 410, (doc, SWARM, "second")])
    page.wait_for_function("() => document.querySelector('#swarm-state').textContent === 'running'")
    for _ in range(80):
        if sum(is_events(r["url"]) for r in seen) >= 4:
            break
        page.wait_for_timeout(100)
    context.close()
    events = [r for r in seen if is_events(r["url"])]
    assert [r["headers"].get("last-event-id") for r in events[:4]] == [None, "first", None, "second"]
    assert all(r["url"].endswith("/api/v1/ledgers/__LEDGER_SLUG__/events") for r in events)
    assert all("token" not in r["url"].lower() and r["headers"].get("x-ledger-token") for r in events)
    assert sorted(r["url"] for r in seen if "/api/" in r["url"] and not is_events(r["url"])) == [
        "http://ledger.test/api/layout",
        "http://ledger.test/api/v1/ledgers/__LEDGER_SLUG__",
    ]


def test_a_failed_stream_marks_the_page_offline_and_the_next_snapshot_recovers_it(browser):
    doc = {**DOC, "_meta": {"rev": 3}}
    context, page, _ = stub_page(browser, [503, (doc, SWARM, "back")])
    page.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('Could not read')")
    assert "offline" in page.locator("#status").get_attribute("class")
    page.wait_for_function("() => document.querySelector('#swarm-state').textContent === 'running'", timeout=5000)
    page.wait_for_function("() => !document.querySelector('#status').className.includes('offline')")
    assert page.locator("#swarm-note").text_content() == ""
    context.close()


@pytest.fixture
def live(monkeypatch):
    content = {"title": "Events", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content, "swarm"), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    hosts = {f"127.0.0.1:{httpd.server_address[1]}"}
    status = {"value": {**SWARM, "config": {**SWARM["config"], "state": "running"}}}
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "ALLOWED_HOSTS", hosts)
    monkeypatch.setattr(server, "ALLOWED_ORIGINS", {f"http://{host}" for host in hosts})
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: status["value"])
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.5)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", status
    httpd.shutdown()
    httpd.server_close()


def note(n):
    return {"op": "add", "thread": "notes", "id": f"n-{n}", "text": f"streamed note {n}"}


def test_tabs_get_ledger_and_swarm_changes_without_polling_the_document(browser, live):
    base, status = live
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    requests = []
    context.on("request", lambda r: requests.append((r.method, r.url.removeprefix(base))))
    tabs = [context.new_page(), context.new_page()]
    for tab in tabs:
        tab.goto(f"{base}/{SLUG}")
        tab.wait_for_function("() => document.querySelector('#swarm-state').textContent === 'running'")
    assert server.HUB.channels[SLUG].subscribers == 2
    server.repository.apply_ops(SLUG, ops=[note(1)])
    status["value"] = {**status["value"], "config": {**status["value"]["config"], "state": "paused"}}
    server.sample_streams()
    for tab in tabs:
        tab.wait_for_function("() => document.body.textContent.includes('streamed note 1')", timeout=5000)
        tab.wait_for_function("() => document.querySelector('#swarm-state').textContent === 'paused'", timeout=5000)
    time.sleep(3)
    reads = [(method, path) for method, path in requests if path.startswith("/api/") and path != "/api/layout"]
    assert sorted(reads) == sorted([("GET", f"/api/v1/ledgers/{SLUG}/events"), ("GET", f"/api/v1/ledgers/{SLUG}")] * 2)
    context.close()
    for _ in range(100):
        if not server.HUB.watched():
            break
        time.sleep(0.05)
    assert server.HUB.watched() == []
