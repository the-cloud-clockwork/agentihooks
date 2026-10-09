import json
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8765/alerts-page"
WARNING = "phase p1 description has 120 words, limit 100"
ALERTS = [
    {"id": "al-3-0", "text": WARNING, "source": "size", "target": "master", "state": "open", "at": 1_000, "rev": 3},
    {
        "id": "al-4-0",
        "text": "phase id p1 is already taken",
        "source": "sync",
        "target": "operator",
        "state": "claimed",
        "claimed_by": "boss",
        "at": 2_000,
        "rev": 4,
    },
    {"id": "al-2-0", "text": "old", "source": "size", "target": "master", "state": "done", "at": 500, "rev": 2},
]
DOC = {"title": "Alerts", "overview": "Alerts sit under the bell.", "phases": [], "alerts": ALERTS}
SERVER = {**DOC, "_meta": {"rev": 5, "warnings": [WARNING], "events": []}}


@pytest.fixture
def tab(browser, request):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    html = shell_html()
    html = html.replace("__LEDGER_SLUG__", "alerts-page").replace(
        "__LEDGER_PALETTE__", (ROOT / "scripts/swarm_ledger/palette.css").read_text()
    )
    sent, server = [], json.loads(json.dumps(SERVER))

    def answer(route):
        if "/api/" not in route.request.url:
            return route.fulfill(body=html, content_type="text/html")
        if "/api/swarm/" in route.request.url:
            return route.fulfill(body="null", content_type="application/json")
        if not route.request.url.endswith("/api/alerts-page"):
            return route.fulfill(json={})
        if route.request.method == "PUT":
            for op in json.loads(route.request.post_data)["ops"]:
                sent.append(op)
                row = next(a for a in server["alerts"] if a["id"] == op.get("target"))
                if op["op"] == "alert_claim":
                    row.update(state="claimed", claimed_by="operator")
                elif op["op"] == "alert_close":
                    row.update(state="done", outcome=op["outcome"])
            server["_meta"]["rev"] += 1
        return route.fulfill(json={**server, "rejected": []})

    context.route("**/*", answer)
    serve_modules(context, ledger_state(DOC))
    context.add_init_script(
        """
        const listen = EventTarget.prototype.addEventListener;
        EventTarget.prototype.addEventListener = function(type, callback, options) {
            if (type === "toggle" && this.id === "alert-fold") {
                const original = callback;
                callback = event => setTimeout(() => original.call(this, event), DELAY);
            }
            return listen.call(this, type, callback, options);
        };
        """.replace("DELAY", str(getattr(request, "param", 0)))
    )
    page = context.new_page()
    page.goto(URL)
    page.set_default_timeout(2000)
    page.wait_for_function("document.getElementById('status').textContent !== 'loading'")
    page.sent = sent
    yield page
    context.close()


def test_the_header_shows_no_warning_text(tab):
    assert tab.locator("#banner").inner_text() == ""
    assert WARNING not in tab.locator("header").inner_text()


def test_the_alert_icon_sits_directly_under_the_bell_with_a_count(tab):
    rail = tab.locator("#icon-strip > *").evaluate_all("els => els.map((el) => el.id)")
    assert rail[rail.index("bell") + 1] == "alert-fab"
    bell, alert = tab.locator("#bell").bounding_box(), tab.locator("#alert-fab").bounding_box()
    assert alert["x"] == pytest.approx(bell["x"]) and alert["y"] > bell["y"]
    assert tab.locator("#alert-badge").inner_text() == "2"


def test_the_panel_lists_open_alerts_and_who_holds_a_claimed_one(tab):
    tab.locator("#alert-fab").click()
    assert tab.locator("#alert-panel").is_visible()
    rows = tab.locator("#alerts > li")
    assert rows.count() == 2
    assert "Claimed by boss" in tab.locator("#alert-al-4-0").inner_text()
    assert WARNING in tab.locator("#alert-al-3-0").inner_text()


def test_claim_and_close_from_the_panel_send_their_ops(tab):
    tab.locator("#alert-fab").click()
    row = tab.locator("#alert-al-3-0")
    row.get_by_role("button", name="Claim").click()
    assert "Claimed by operator" in row.inner_text()
    row.get_by_role("button", name="Mark done").click()
    row.get_by_label("Outcome").fill("Trimmed the phase")
    row.get_by_role("button", name="Save").click()
    tab.wait_for_function("document.querySelectorAll('#alerts > li').length === 1")
    tab.wait_for_timeout(300)
    kinds = [(op["op"], op["target"], op.get("outcome")) for op in tab.sent if op["op"].startswith("alert_")]
    assert kinds == [("alert_claim", "al-3-0", None), ("alert_close", "al-3-0", "Trimmed the phase")]


@pytest.mark.parametrize("tab", [0, 250], indirect=True)
def test_the_panel_fold_is_remembered_after_a_reload(tab):
    tab.locator("#alert-fab").click()
    tab.locator("#alert-fold > summary").click()
    assert tab.locator("#alert-fold").evaluate("el => el.open") is False
    tab.wait_for_function(
        """JSON.parse(localStorage.getItem('plan-ledger:alerts-page:fold') || '{}')
            ['alert-fold'] === false"""
    )
    tab.reload()
    tab.wait_for_function("document.getElementById('status').textContent !== 'loading'")
    tab.locator("#alert-fab").click()
    assert tab.locator("#alert-fold").evaluate("el => el.open") is False
