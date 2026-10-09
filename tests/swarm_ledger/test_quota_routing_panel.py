import json

import pytest

from tests.swarm_ledger.test_swarm_layout import Page, browser, status

assert browser

SETTINGS = {"claude-api-weight": 25, "codex-api-weight": 0, "claude-api-max-sessions": 3}
API_ROW = {
    "agent": "claude",
    "account": "openrouter",
    "kind": "api",
    "five_hour_left": None,
    "five_hour_resets_at": None,
    "seven_day_left": None,
    "seven_day_resets_at": None,
    "sessions": 1,
    "cap": 3,
    "weight": 25,
}


class RoutingPage(Page):
    def __init__(self, browser, payload, width, settings, refusal=None):
        self.settings, self.refusal, self.patches = dict(settings), refusal, []
        super().__init__(browser, payload, width)

    def route(self, route, html):
        request = route.request
        if request.url.endswith("/api/v1/routing/settings"):
            if request.method == "PATCH":
                body = json.loads(request.post_data)
                self.patches.append((body, request.headers.get("x-ledger-token"), request.headers.get("x-ledger-slug")))
                if self.refusal:
                    return route.fulfill(status=403, json={"error": {"code": "forbidden", "message": self.refusal}})
                self.settings = {k: v for k, v in {**self.settings, **body}.items() if v is not None}
            return route.fulfill(json={"data": self.settings, "revision": "r"})
        return super().route(route, html)


def payload():
    sw = status()
    sw["quota"] = {**sw["quota"], "rows": [{**row, "kind": "subscription"} for row in sw["quota"]["rows"]] + [API_ROW]}
    return sw


@pytest.fixture
def routing_page(browser):
    pages = []

    def make(settings=SETTINGS, refusal=None, width=1920):
        page = RoutingPage(browser, payload(), width, settings, refusal)
        page.tab.locator('#swarm-quota input[data-routing="claude-api-weight"]').wait_for(timeout=3000)
        pages.append(page)
        return page

    yield make
    for page in pages:
        assert page.errors == []
        page.context.close()


def inputs(page):
    return page.tab.eval_on_selector_all(
        "#swarm-quota input[data-routing]",
        "els => els.map(e => [e.dataset.routing, e.value, e.getAttribute('aria-label')])",
    )


def test_quota_rows_show_kind_weight_and_cap_with_inputs_only_on_api_rows(routing_page):
    page = routing_page()
    assert page.table("swarm-quota") == [
        ["tccgma", "claude", "SUBSCRIPTION", "—", "92%", "53m", "78%", "4d15h", "—", "2", "—", "6", ""],
        ["luna", "claude", "SUBSCRIPTION", "—", "64%", "2h05m", "51%", "6d00h", "—", "1", "—", "4", "MASTER"],
        ["default", "codex", "SUBSCRIPTION", "—", "—", "—", "61%", "3d10h", "—", "0", "—", "—", ""],
        ["openrouter", "claude", "API", "—", "—", "—", "—", "—", "—", "1", "%", "", ""],
    ]
    assert inputs(page) == [
        ["claude-api-weight", "25", "claude api weight"],
        ["claude-api-max-sessions", "3", "claude api cap"],
    ]


def test_an_unset_cap_reads_empty_with_none_as_its_placeholder(routing_page):
    page = routing_page({"claude-api-weight": 10, "codex-api-weight": 0})
    cap = page.tab.locator('#swarm-quota input[data-routing="claude-api-max-sessions"]')
    assert (cap.input_value(), cap.get_attribute("placeholder")) == ("", "none")


def test_editing_the_weight_writes_it_as_the_operator_and_shows_the_stored_value(routing_page):
    page = routing_page()
    weight = page.tab.locator('#swarm-quota input[data-routing="claude-api-weight"]')
    weight.fill("40")
    weight.press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('acknowledged')")
    assert page.patches == [({"claude-api-weight": 40}, "__LEDGER_TOKEN__", "__LEDGER_SLUG__")]
    assert dict((key, value) for key, value, _ in inputs(page))["claude-api-weight"] == "40"


def test_clearing_the_cap_sends_null(routing_page):
    page = routing_page()
    cap = page.tab.locator('#swarm-quota input[data-routing="claude-api-max-sessions"]')
    cap.fill("")
    cap.press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('acknowledged')")
    assert [body for body, _, _ in page.patches] == [{"claude-api-max-sessions": None}]
    assert dict((key, value) for key, value, _ in inputs(page))["claude-api-max-sessions"] == ""


def test_a_value_that_is_not_a_whole_number_reaches_the_server_as_text_to_be_refused(routing_page):
    page = routing_page(refusal="Invalid value for claude-api-weight")
    weight = page.tab.locator('#swarm-quota input[data-routing="claude-api-weight"]')
    weight.fill("lots")
    weight.press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('Invalid value')")
    assert [body for body, _, _ in page.patches] == [{"claude-api-weight": "lots"}]
    assert dict((key, value) for key, value, _ in inputs(page))["claude-api-weight"] == "25"


def test_a_refused_write_names_the_reason_and_restores_the_stored_value(routing_page):
    page = routing_page(refusal="Routing settings need the operator")
    weight = page.tab.locator('#swarm-quota input[data-routing="claude-api-weight"]')
    weight.fill("90")
    weight.press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('need the operator')")
    assert "bad" in page.tab.get_attribute("#swarm-note", "class")
    assert dict((key, value) for key, value, _ in inputs(page))["claude-api-weight"] == "25"


def test_the_quota_box_folds_on_its_header_and_remembers_it(routing_page):
    page = routing_page()
    assert page.tab.locator("#quota-table").is_visible()
    page.tab.locator("#quota-fold > summary h3").click()
    assert not page.tab.locator("#quota-table").is_visible()
    page.tab.wait_for_function("() => Object.values(localStorage).some((v) => v.includes('\"quota-fold\":false'))")
    page.tab.reload()
    page.tab.locator("#swarm-agents tr").first.wait_for(timeout=3000)
    assert page.tab.eval_on_selector("#quota-fold", "d => d.open") is False


def test_the_refresh_icon_in_the_folding_header_does_not_fold_the_box(routing_page):
    page = routing_page()
    page.tab.locator("#quota-refresh").click()
    assert page.tab.eval_on_selector("#quota-fold", "d => d.open") is True


def test_the_wider_quota_table_still_fits_its_box_at_1920(routing_page):
    page = routing_page()
    scroll = page.tab.eval_on_selector("#quota-box .sw-scroll", "s => [s.scrollWidth, s.clientWidth]")
    assert scroll[0] == scroll[1]


def test_the_quota_table_lines_up_with_the_agents_table_above_it(routing_page):
    page = routing_page()
    assert abs(page.box("quota-table")["x"] - page.box("agents-table")["x"]) < 1
