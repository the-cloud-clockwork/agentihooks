import copy
import json
import re

from scripts.swarm import lifecycle
from tests.swarm_ledger import test_swarm_layout as layout
from tests.swarm_ledger.test_swarm_layout import Page, status

browser = layout.browser
open_page = layout.open_page

AGENT = "eng-58"
OP = re.compile(r"l-[0-9a-f]{10}")
SEEN = {"incarnation": "a1b2c3d4e5f60718", "generation": 2, "task_revision": 4}
RECONCILING = "waiting to reconcile an earlier control by its operation id"


def controls():
    return [
        {
            "action": c.action,
            "label": c.label,
            "scope": c.scope,
            "destructive": c.destructive,
            "consequence": c.consequence,
        }
        for c in lifecycle.CONTROLS
    ]


def with_lifecycle(destructive=True, operations=(), force_stop="", terminate=False):
    payload = status()
    disabled = {"attach": "", "drain": "", "cancel": "", "force_stop": force_stop}
    payload["lifecycle"] = {
        "controls": controls(),
        "destructive": destructive,
        "agents": {AGENT: {**SEEN, "pending": "", "disabled": disabled, "terminate": terminate}},
        "operations": list(operations),
        "conflicts": {},
        "ledger_control_conflicts_total": 0,
    }
    return payload


def acts(page):
    return page.tab.locator(f"#agent-{AGENT} .sw-acts")


def button(page, label):
    return acts(page).get_by_role("button", name=f"{label} {AGENT}")


def press(page, label, double=False):
    page.tab.locator(f"#agent-{AGENT}").hover()
    target = button(page, label)
    target.dblclick() if double else target.click()


def wait_settled(page):
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")


def test_an_agent_row_offers_attach_drain_cancel_and_force_stop_with_their_consequences(open_page):
    page = open_page(with_lifecycle(force_stop="a kubernetes agent has no force stop"))
    assert acts(page).inner_text().split("\n") == ["message", "attach", "drain", "cancel", "force stop"]
    by_label = {c.label: c for c in lifecycle.CONTROLS}
    for label in ("attach", "drain", "cancel"):
        control = by_label[label]
        assert button(page, label).get_attribute("title") == f"{control.scope}: {control.consequence}"
        assert button(page, label).is_enabled()
    stop = button(page, "force stop")
    assert stop.is_disabled()
    assert stop.get_attribute("title") == "a kubernetes agent has no force stop"
    assert "danger" in button(page, "cancel").get_attribute("class")
    assert "danger" not in button(page, "drain").get_attribute("class")
    pause = by_label["pause"]
    assert page.tab.locator('#swarm-ctl [data-swarm="pause"]').get_attribute("title") == (
        f"one swarm: {pause.consequence}"
    )


def test_a_page_without_lifecycle_offers_keeps_the_terminate_button(open_page):
    page = open_page(status())
    assert acts(page).inner_text().split("\n") == ["message", "terminate"]


def test_the_rollback_switch_brings_terminate_back_beside_the_offers(open_page):
    page = open_page(
        with_lifecycle(destructive=False, force_stop="force stop is turned off; terminate still works", terminate=True)
    )
    assert acts(page).inner_text().split("\n") == ["message", "attach", "drain", "cancel", "force stop", "terminate"]


def test_a_supported_force_stop_replaces_terminate(open_page):
    page = open_page(with_lifecycle())
    assert acts(page).inner_text().split("\n") == ["message", "attach", "drain", "cancel", "force stop"]
    assert button(page, "force stop").is_enabled()


def test_drain_sends_the_generation_and_task_revision_the_page_showed(open_page):
    page = open_page(with_lifecycle())
    press(page, "drain")
    wait_settled(page)
    [sent] = page.puts
    assert OP.fullmatch(sent.pop("op"))
    assert sent == {"action": "lifecycle", "control": "drain", "agent": AGENT, **SEEN}


def test_a_double_click_sends_one_request(open_page):
    page = open_page(with_lifecycle())
    press(page, "drain", double=True)
    wait_settled(page)
    assert len(page.puts) == 1


def test_a_destructive_control_asks_first_and_names_its_consequence(open_page):
    page = open_page(with_lifecycle())
    asked = []

    def answer(dialog):
        asked.append(dialog.message)
        dialog.dismiss() if len(asked) == 1 else dialog.accept()

    page.tab.on("dialog", answer)
    press(page, "cancel")
    assert page.puts == []
    press(page, "cancel")
    wait_settled(page)
    consequence = next(c.consequence for c in lifecycle.CONTROLS if c.action == "cancel")
    assert asked == [f"cancel {AGENT}? {consequence}"] * 2
    assert [put["control"] for put in page.puts] == ["cancel"]


class LostAck(Page):
    def __init__(self, chromium, payload, width, drop):
        self.drop = drop
        super().__init__(chromium, payload, width)

    def route(self, route, html):
        request = route.request
        if "/swarm/" in request.url and request.method == "PUT" and self.drop:
            self.drop -= 1
            self.puts.append(json.loads(request.post_data))
            return route.abort()
        return super().route(route, html)


def lost(chromium, drop=1):
    page = LostAck(chromium, with_lifecycle(), 1440, drop)
    press(page, "drain")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('unreachable')")
    return page


def test_a_lost_acknowledgement_holds_the_agent_until_its_operation_is_reconciled(browser):
    page = lost(browser)
    try:
        assert len(page.puts) == 1
        for label in ("attach", "drain", "cancel"):
            assert button(page, label).is_disabled()
            assert button(page, label).get_attribute("title") == RECONCILING
        first = page.puts[0]
        page.payload = with_lifecycle()
        page.tab.reload()
        page.tab.locator(f"#agent-{AGENT}").wait_for()
        page.tab.wait_for_timeout(500)
        assert [put["op"] for put in page.puts] == [first["op"], first["op"]]
        assert page.puts[1] == first
    finally:
        page.context.close()


def test_a_lost_acknowledgement_the_server_recorded_is_not_sent_again(browser):
    page = lost(browser)
    try:
        op = page.puts[0]["op"]
        page.payload = with_lifecycle(operations=[{"op": op, "control": "drain", "agent": AGENT, "state": "applied"}])
        page.tab.reload()
        page.tab.locator(f"#agent-{AGENT}").wait_for()
        page.tab.wait_for_timeout(500)
        assert len(page.puts) == 1
        assert button(page, "drain").is_enabled()
        assert page.tab.evaluate("() => sessionStorage.length") == 0
    finally:
        page.context.close()


def test_a_refused_control_is_not_held_for_reconciling(open_page):
    payload = copy.deepcopy(with_lifecycle())
    page = open_page(payload)
    page.tab.route(
        "**/swarm/controls",
        lambda route: (
            route.fulfill(status=409, body="the page is stale: this agent was replaced, finished or moved task")
            if route.request.method == "PUT"
            else route.fallback()
        ),
    )
    press(page, "drain")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('stale')")
    assert page.tab.evaluate("() => sessionStorage.length") == 0
