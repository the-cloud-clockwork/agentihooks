import pytest

from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]

STOP_NOW = "Retire every agent now. Work in progress stays in its worktree."
CLOSE = "Write the summary, retire every agent and close this ledger. Reopen brings it back."


def with_profiles(**changes):
    payload = status(**changes)
    payload["agents"][0]["profile"] = "master"
    payload["agents"][1]["profile"] = "engineer"
    return payload


def answer(page, accept):
    seen = []

    def handle(dialog):
        seen.append(dialog.message)
        dialog.accept() if accept else dialog.dismiss()

    page.tab.once("dialog", handle)
    return seen


def settle(page):
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")


def row(page, name):
    return page.tab.locator("#swarm-agents tr", has_text=name)


def acts_opacity(page, name):
    return row(page, name).locator(".sw-acts").evaluate("el => getComputedStyle(el).opacity")


def test_agents_table_has_a_profile_column(open_page):
    page = open_page(with_profiles())
    heads = page.tab.eval_on_selector_all("#agents-table thead th", "ths => ths.map(t => t.innerText.trim())")
    assert heads[:8] == ["agent", "lane", "profile", "overlays", "model", "task", "state", "age"]
    assert [cells[:8] for cells in page.table("swarm-agents")] == [
        ["master-1", "—", "master", "—", "opus high", "—", "LIVE", "2h 0m"],
        ["eng-58", "eng", "engineer", "—", "opus high", "pb10", "IDLE", "1m"],
    ]
    assert [cells[2] for cells in open_page().table("swarm-agents")] == ["—", "—"]


def test_row_actions_hide_at_rest_and_show_on_hover_and_keyboard_focus(open_page):
    page = open_page(with_profiles())
    assert acts_opacity(page, "eng-58") == "0"
    row(page, "eng-58").hover()
    assert acts_opacity(page, "eng-58") == "1"
    page.tab.mouse.move(0, 0)
    assert acts_opacity(page, "eng-58") == "0"
    row(page, "eng-58").get_by_role("button", name="terminate").focus()
    assert acts_opacity(page, "eng-58") == "1"


def test_row_actions_stay_visible_on_a_touch_screen(open_page):
    page = open_page(with_profiles(), 390, has_touch=True, is_mobile=True)
    assert acts_opacity(page, "eng-58") == "1"


def test_message_opens_the_chat_addressed_to_the_row(open_page):
    page = open_page(with_profiles())
    row(page, "eng-58").hover()
    row(page, "eng-58").get_by_role("button", name="message").click()
    assert page.tab.locator("#chat-panel").is_visible()
    assert page.tab.locator("#chat-to").input_value() == "eng-58"
    row(page, "master-1").hover()
    row(page, "master-1").get_by_role("button", name="message").click()
    assert page.tab.locator("#chat-to").input_value() == ""
    assert page.puts == []


def test_terminate_asks_then_sends_only_once_confirmed(open_page):
    page = open_page(with_profiles())
    button = row(page, "eng-58").get_by_role("button", name="terminate")
    row(page, "eng-58").hover()
    asked = answer(page, accept=False)
    button.click()
    assert asked == ["Terminate eng-58? Work in progress stays in its worktree."]
    assert page.puts == []
    row(page, "eng-58").hover()
    answer(page, accept=True)
    button.click()
    settle(page)
    assert page.puts == [{"action": "terminate", "name": "eng-58"}]


@pytest.mark.parametrize(
    ("label", "action", "consequence"), [("stop now", "stop_now", STOP_NOW), ("close ledger", "close", CLOSE)]
)
def test_header_stop_now_and_close_ledger_ask_then_send(open_page, label, action, consequence):
    page = open_page()
    button = page.tab.locator("#swarm-ctl").get_by_role("button", name=label)
    head, control = page.box("swarm-head"), button.bounding_box()
    assert head["y"] <= control["y"] and control["y"] + control["height"] <= head["y"] + head["height"]
    asked = answer(page, accept=False)
    button.click()
    assert asked == [consequence]
    assert page.puts == []
    answer(page, accept=True)
    button.click()
    settle(page)
    assert page.puts == [{"action": action}]


def test_stop_doctor_still_asks_then_sends(open_page):
    page = open_page(status(doctor={"slug": "rig-doctor", "state": "running", "last_check": 0, "findings": 0}))
    button = page.tab.locator("[data-swarm=doctor_stop]")
    asked = answer(page, accept=False)
    button.click()
    assert asked == ["Stop the Doctor crew and close its linked ledger."]
    assert page.puts == []
    answer(page, accept=True)
    button.click()
    settle(page)
    assert page.puts == [{"action": "doctor_stop"}]


def test_stop_now_is_disabled_once_the_swarm_is_stopped(open_page):
    payload = status()
    payload["config"]["state"] = "stopped"
    page = open_page(payload)
    state = page.tab.eval_on_selector_all(
        "#swarm-ctl button", "bs => Object.fromEntries(bs.map(b => [b.textContent, b.disabled]))"
    )
    assert state["stop now"] is True and state["close ledger"] is False
