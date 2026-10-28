import json

import pytest

from tests.swarm_ledger.test_swarm_layout import Page, status
from tests.swarm_ledger.test_swarm_layout import browser as chromium_browser

browser = chromium_browser

NOW_MS = 1_780_000_000_000


def hive(hive_id, live, **fields):
    return {
        "id": hive_id,
        "name": hive_id,
        "live": live,
        "ui": "yes",
        "ephemeral": "no",
        "excluded": [],
        "prefer": {"eng": 1},
        "max_agents": 4,
        "agents": 1,
        "slots": [{"harness": "claude", "account": "luna", "kind": "subscription", "cap": 6, "agents": 1}],
        "master": None,
        "heartbeat_at": 0,
        **fields,
    }


HIVES = [
    hive("desk", True, master={"slug": "rig", "name": "master@rig-1"}, agents=2),
    hive("pod", True, ui="no", excluded=["master", "qa"], prefer={}, max_agents=2, agents=0),
    hive("lab", False, ephemeral="yes", agents=0),
]


class HivesPage(Page):
    def __init__(self, browser, payload, width, refusal=None, read_status=200):
        self.hives, self.refusal, self.patches = json.loads(json.dumps(HIVES)), refusal, []
        self.read_status = read_status
        super().__init__(browser, payload, width)

    def route(self, route, html):
        request = route.request
        if "/api/v1/hives" in request.url:
            if request.method == "PATCH":
                hive_id = request.url.rsplit("/", 1)[1]
                body = json.loads(request.post_data)
                self.patches.append(
                    (hive_id, body, request.headers.get("x-ledger-token"), request.headers.get("x-ledger-slug"))
                )
                if self.refusal == "abort":
                    return route.abort()
                if isinstance(self.refusal, int):
                    return route.fulfill(status=self.refusal, body="not json")
                if self.refusal:
                    return route.fulfill(
                        status=400, json={"error": {"code": "schema_invalid", "message": self.refusal}}
                    )
                found = next(h for h in self.hives if h["id"] == hive_id)
                found.update(body)
                return route.fulfill(json={"data": found, "revision": "r"})
            return route.fulfill(status=self.read_status, json={"data": self.hives, "revision": "r"})
        return super().route(route, html)


@pytest.fixture
def hives_page(browser):
    pages = []

    def make(refusal=None, width=1920):
        page = HivesPage(browser, status(), width, refusal)
        page.tab.locator("#swarm-hives tr#hive-desk").wait_for(timeout=3000)
        pages.append(page)
        return page

    yield make
    for page in pages:
        assert page.errors == []
        page.context.close()


def fields(page, hive_id):
    return page.tab.eval_on_selector_all(
        f"#hive-{hive_id} [data-hive-field]", "els => els.map(e => [e.dataset.hiveField, e.value])"
    )


def wait_for(page, check):
    for _ in range(100):
        if check():
            return
        page.tab.wait_for_timeout(100)


def save(page, hive_id, field, value, shown):
    selector = f'#hive-{hive_id} [data-hive-field="{field}"]'
    box = page.tab.locator(selector)
    old = box.element_handle()
    if field in ("ui", "ephemeral"):
        box.select_option(value)
    else:
        box.fill(value)
        box.press("Enter")
    page.tab.wait_for_function(
        "([old, selector, shown]) => !old.isConnected && document.querySelector(selector).value === shown",
        arg=[old, selector, shown],
    )


def test_each_hive_shows_only_the_requested_columns(hives_page):
    page = hives_page()
    rows = page.table("swarm-hives")
    heads = [text.lower() for text in page.tab.locator("#hives-table th").all_text_contents()]
    assert heads == ["hive", "state", "ui", "ephemeral", "excluded", "max agents", "agents", "master", "actions"]
    assert [row[0] for row in rows] == ["desk", "pod", "lab"]
    assert [row[1] for row in rows] == ["LIVE", "LIVE", "QUIET"]
    assert all(len(row) == 9 for row in rows)
    assert [row[6] for row in rows] == ["2/4", "0/2", "0/4"]
    assert [row[7] for row in rows] == ["master@rig-1", "—", "—"]
    assert [row[8] for row in rows] == ["MOVE MASTER"] * 3
    assert "luna" not in page.text("#swarm-hives")
    assert fields(page, "pod") == [
        ["ui", "no"],
        ["ephemeral", "no"],
        ["excluded", "master,qa"],
        ["max_agents", "2"],
    ]
    assert page.tab.locator("[data-hive-field=prefer]").count() == 0
    assert page.text("#hives-count").lower() == "2 live · 3 hives"


def test_a_failed_read_says_the_hives_could_not_be_read(browser):
    page = HivesPage(browser, status(), 1920, read_status=500)
    try:
        page.tab.wait_for_function(
            "() => document.querySelector('#swarm-hives').textContent === 'Could not read hives.'", timeout=3000
        )
        assert page.text("#hives-count") == "—"
        assert page.errors == []
    finally:
        page.context.close()


def test_the_quota_header_has_only_its_title_and_refresh_control(hives_page):
    page = hives_page()
    header = page.tab.locator("#quota-fold > summary")
    assert header.inner_text().strip().lower() == "quota"
    assert header.locator("h3").all_text_contents() == ["Quota"]
    assert header.locator("button").count() == 1
    assert header.locator("#quota-refresh").get_attribute("aria-label") == "Refresh quota"
    assert header.locator(".sw-count").count() == 0


def test_editing_max_agents_writes_it_as_the_operator(hives_page):
    page = hives_page()
    box = page.tab.locator('#hive-pod [data-hive-field="max_agents"]')
    box.fill("5")
    box.press("Enter")
    wait_for(page, lambda: page.patches)
    assert page.patches == [("pod", {"max_agents": 5}, "__LEDGER_TOKEN__", "__LEDGER_SLUG__")]
    page.tab.wait_for_function("() => document.querySelector('#hive-pod').cells[6].textContent === '0/5'")


def test_excluded_and_flags_are_sent_in_their_api_shape(hives_page):
    page = hives_page()
    save(page, "pod", "excluded", "master, qa,frontend", "master,qa,frontend")
    save(page, "lab", "ephemeral", "no", "no")
    assert [(hive_id, body) for hive_id, body, _, _ in page.patches] == [
        ("pod", {"excluded": ["master", "qa", "frontend"]}),
        ("lab", {"ephemeral": "no"}),
    ]


def test_a_refused_write_names_the_reason_and_keeps_the_stored_value(hives_page):
    page = hives_page(refusal="excluded takes lane roles or profiles, not 'a b'")
    excluded = page.tab.locator('#hive-pod [data-hive-field="excluded"]')
    excluded.fill("a b")
    excluded.press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('lane roles')")
    assert page.text("#swarm-note").startswith("Could not set hive")
    assert dict(fields(page, "pod"))["excluded"] == "master,qa"


@pytest.mark.parametrize(("refusal", "reason"), [("abort", "ledger server unreachable"), (500, "server answered 500")])
def test_a_failed_write_without_a_reason_names_what_went_wrong(hives_page, refusal, reason):
    page = hives_page(refusal=refusal)
    box = page.tab.locator('#hive-pod [data-hive-field="max_agents"]')
    box.fill("5")
    box.press("Enter")
    page.tab.wait_for_function(f"() => document.querySelector('#swarm-note').textContent.includes('{reason}')")
    assert page.text("#swarm-note") == f"Could not set hive: {reason}. Try again or ask the master."
    assert dict(fields(page, "pod"))["max_agents"] == "2"


def test_a_hive_without_a_name_excluded_prefer_or_slots_shows_its_id_and_empty_settings(browser):
    page = HivesPage(browser, status(), 1920)
    page.hives = [{"id": "bare", "live": True, "ui": "no", "ephemeral": "yes", "max_agents": 3, "agents": 0}]
    page.tab.reload()
    try:
        page.tab.locator("#swarm-hives tr#hive-bare").wait_for(timeout=3000)
        assert page.table("swarm-hives")[0][0] == "bare"
        assert fields(page, "bare")[2:] == [["excluded", ""], ["max_agents", "3"]]
        assert page.errors == []
    finally:
        page.context.close()


def test_move_master_orders_the_move_and_is_off_for_the_masters_hive_and_quiet_hives(hives_page):
    page = hives_page()
    buttons = page.tab.eval_on_selector_all(
        "#swarm-hives button[data-move-master]", "bs => bs.map(b => [b.dataset.moveMaster, b.disabled])"
    )
    assert buttons == [["desk", True], ["pod", False], ["lab", True]]
    page.tab.locator("#hive-pod button[data-move-master]").click()
    wait_for(page, lambda: page.puts)
    assert page.puts == [{"action": "master_move", "hive": "pod"}]


def test_the_hives_box_folds_on_its_header_and_remembers_it(hives_page):
    page = hives_page()
    assert page.tab.eval_on_selector("#hives-fold", "d => d.open") is True
    page.tab.locator("#hives-fold > summary h3").click()
    assert page.tab.eval_on_selector("#hives-fold", "d => d.open") is False
    page.tab.wait_for_function("() => Object.values(localStorage).some((v) => v.includes('\"hives-fold\":false'))")


def test_the_hives_table_fits_its_box_at_1920(hives_page):
    page = hives_page()
    scroll = page.tab.eval_on_selector("#hives-box .sw-scroll", "s => [s.scrollWidth, s.clientWidth]")
    assert scroll[0] <= scroll[1]
