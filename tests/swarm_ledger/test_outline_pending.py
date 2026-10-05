import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import new_ledger  # noqa: E402

TEMPLATE = SCRIPTS / "template.html"
URL = "http://ledger.test/swarm-buildout"
API = "http://ledger.test/api/swarm-buildout"


def notice(nid, item, text):
    return {"id": nid, "item": item, "label": "Reply", "text": text, "by": "eng-1", "at": 1}


DOC = {
    "title": "Pending marks",
    "overview": "o",
    "phases": [{"id": "p1", "title": "phase one", "done": False}, {"id": "p2", "title": "phase two", "done": True}],
    "followups": [{"id": "f1", "text": "rotate the key", "done": False}],
    "questions": [{"id": "q1", "text": "which port", "answers": []}],
    "notifications": [
        notice("n1", "followups/f1", "first on the follow up"),
        notice("n2", "followups/f1", "second on the follow up"),
        notice("n3", "phases/p2", "on phase two"),
        notice("n4", "chat", "on the chat"),
    ],
}


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def server():
    doc = json.loads(json.dumps(DOC))
    doc["_meta"] = {"rev": 1}
    return doc


@pytest.fixture
def tab(browser, server):
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    html = new_ledger.render(DOC, "swarm-buildout", 8765)

    def handle(route):
        request = route.request
        if request.url == URL:
            return route.fulfill(body=html, content_type="text/html")
        if request.url != API:
            return route.abort()
        if request.method == "PUT":
            for op in json.loads(request.post_data)["ops"]:
                if op["op"] == "notification_clear":
                    keep = (
                        [] if op["target"] == "all" else [n for n in server["notifications"] if n["id"] != op["target"]]
                    )
                    server["notifications"] = keep
            server["_meta"]["rev"] += 1
        route.fulfill(body=json.dumps(server), content_type="application/json")

    context.route("**/*", handle)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(URL)
    yield page
    context.close()


def marked(tab):
    return tab.evaluate(
        """() => [...document.querySelectorAll("#outline a[data-target]")]
             .filter((a) => getComputedStyle(a, "::after").content === '"+"')
             .map((a) => a.dataset.target)"""
    )


def clear(tab, text):
    tab.evaluate(
        """(text) => {
          const row = [...document.querySelectorAll("#notifs .notif-row")].find((r) => r.textContent.includes(text));
          [...row.querySelectorAll("button")].find((b) => b.textContent === "Clear").click();
        }""",
        text,
    )
    tab.wait_for_timeout(50)


def test_a_pending_notification_puts_the_plus_on_its_item_only(tab):
    assert sorted(marked(tab)) == ["item-followups-f1", "item-phases-p2"]


def test_an_item_keeps_the_plus_until_its_last_notification_is_cleared(tab):
    clear(tab, "first on the follow up")
    assert sorted(marked(tab)) == ["item-followups-f1", "item-phases-p2"]
    clear(tab, "second on the follow up")
    assert marked(tab) == ["item-phases-p2"]


def test_clear_all_removes_every_plus(tab):
    assert marked(tab)
    tab.evaluate("""() => document.getElementById("notif-clear-all").click()""")
    tab.wait_for_timeout(50)
    assert marked(tab) == []


def test_a_notification_from_a_page_update_adds_the_plus_live(tab, server):
    server["notifications"].append(notice("n5", "questions/q1", "on the question"))
    server["_meta"]["rev"] += 1
    tab.wait_for_function(
        """() => getComputedStyle(document.querySelector('#outline a[data-target="item-questions-q1"]'), "::after").content === '"+"'""",
        timeout=6000,
    )
    assert sorted(marked(tab)) == ["item-followups-f1", "item-phases-p2", "item-questions-q1"]


def test_the_plus_takes_a_palette_colour_and_keeps_the_state_dot(tab):
    colours = tab.evaluate(
        """() => {
          const a = document.querySelector('#outline a[data-target="item-phases-p2"]');
          return { plus: getComputedStyle(a, "::after").color, dot: getComputedStyle(a, "::before").color };
        }"""
    )
    page = TEMPLATE.read_text(encoding="utf-8")
    rule = re.search(r"\.outline \.ol-items a\.pending::after\s*\{([^}]*)\}", page)
    assert rule and "color: var(--pending)" in rule.group(1)
    assert re.search(r"--pending:\s*var\(--[\w-]+\);", (SCRIPTS / "palette.css").read_text(encoding="utf-8"))
    assert colours == {"plus": "rgb(248, 250, 252)", "dot": "rgb(74, 222, 128)"}
