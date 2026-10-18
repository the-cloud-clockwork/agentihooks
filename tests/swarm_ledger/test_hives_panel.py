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
        "roles": ["master", "eng"],
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
    hive("pod", True, ui="no", roles=["eng", "ci"], prefer={}, max_agents=2, agents=0),
    hive("lab", False, ephemeral="yes", agents=0),
]


class HivesPage(Page):
    def __init__(self, browser, payload, width, refusal=None):
        self.hives, self.refusal, self.patches = json.loads(json.dumps(HIVES)), refusal, []
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
                if self.refusal:
                    return route.fulfill(
                        status=400, json={"error": {"code": "schema_invalid", "message": self.refusal}}
                    )
                found = next(h for h in self.hives if h["id"] == hive_id)
                found.update(body)
                return route.fulfill(json={"data": found, "revision": "r"})
            return route.fulfill(json={"data": self.hives, "revision": "r"})
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


def test_each_hive_shows_liveness_tags_roles_prefer_max_agents_slots_and_master(hives_page):
    page = hives_page()
    rows = page.table("swarm-hives")
    assert [row[0] for row in rows] == ["desk", "pod", "lab"]
    assert [row[1] for row in rows] == ["LIVE", "LIVE", "QUIET"]
    assert [row[7] for row in rows] == ["2/4", "0/2", "0/4"]
    assert [row[8] for row in rows] == ["claude luna 1"] * 3
    assert [row[9] for row in rows] == ["master@rig-1", "—", "—"]
    assert fields(page, "pod") == [
        ["ui", "no"],
        ["ephemeral", "no"],
        ["roles", "eng,ci"],
        ["prefer", ""],
        ["max_agents", "2"],
    ]
    assert fields(page, "desk")[3] == ["prefer", "eng:1"]
    assert page.text("#hives-count") == "2 live · 3 hives"


def test_the_quota_count_names_each_hive_with_its_agents_and_max(hives_page):
    page = hives_page()
    page.tab.wait_for_function("() => document.querySelector('#quota-count').textContent.includes('desk 2/4')")
    assert page.text("#quota-count").endswith("desk 2/4 · pod 0/2 · lab 0/4")


def test_editing_max_agents_writes_it_as_the_operator(hives_page):
    page = hives_page()
    box = page.tab.locator('#hive-pod [data-hive-field="max_agents"]')
    box.fill("5")
    box.press("Enter")
    wait_for(page, lambda: page.patches)
    assert page.patches == [("pod", {"max_agents": 5}, "__LEDGER_TOKEN__", "__LEDGER_SLUG__")]
    page.tab.wait_for_function("() => document.querySelector('#hive-pod').cells[7].textContent === '0/5'")


def test_roles_prefer_and_flags_are_sent_in_their_api_shape(hives_page):
    page = hives_page()
    roles = page.tab.locator('#hive-pod [data-hive-field="roles"]')
    roles.fill("eng, ci,plan")
    roles.press("Enter")
    wait_for(page, lambda: len(page.patches) == 1)
    prefer = page.tab.locator('#hive-pod [data-hive-field="prefer"]')
    prefer.fill("eng:2,plan:1")
    prefer.press("Enter")
    wait_for(page, lambda: len(page.patches) == 2)
    page.tab.locator('#hive-lab [data-hive-field="ephemeral"]').select_option("no")
    wait_for(page, lambda: len(page.patches) == 3)
    assert [(hive_id, body) for hive_id, body, _, _ in page.patches] == [
        ("pod", {"roles": ["eng", "ci", "plan"]}),
        ("pod", {"prefer": {"eng": 2, "plan": 1}}),
        ("lab", {"ephemeral": "no"}),
    ]


def test_a_refused_write_names_the_reason_and_keeps_the_stored_value(hives_page):
    page = hives_page(refusal="role qa needs a UI, and this hive has ui=no")
    roles = page.tab.locator('#hive-pod [data-hive-field="roles"]')
    roles.fill("qa")
    roles.press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('needs a UI')")
    assert page.text("#swarm-note").startswith("Could not set hive")
    assert dict(fields(page, "pod"))["roles"] == "eng,ci"


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
