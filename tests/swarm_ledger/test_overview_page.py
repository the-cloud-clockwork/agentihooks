from tests.swarm_ledger.test_freeze_page import browser, page, sent_ops  # noqa: F401


def test_the_pen_edits_the_overview_and_saves_it_with_ctrl_enter(page):  # noqa: F811
    tab, sent = page
    assert tab.locator("#overview-form").is_hidden()
    tab.click("#overview-edit")
    assert tab.locator("#overview").is_hidden() and tab.locator("#overview-edit").is_hidden()
    box = tab.locator("#overview-input")
    box.fill("  A clearer overview  ")
    box.press("Control+Enter")
    assert tab.locator("#overview-form").is_hidden()
    assert sent_ops(tab, sent, "overview_set") == [{"op": "overview_set", "text": "A clearer overview"}]


def test_cancel_and_escape_close_the_editor_without_sending(page):  # noqa: F811
    tab, sent = page
    tab.click("#overview-edit")
    tab.locator("#overview-input").fill("Changed")
    tab.click("#overview-cancel")
    assert tab.locator("#overview-form").is_hidden()
    tab.click("#overview-edit")
    tab.locator("#overview-input").press("Escape")
    assert tab.locator("#overview-form").is_hidden()
    tab.click("#overview-edit")
    tab.click('#overview-form [type="submit"]')
    tab.wait_for_timeout(300)
    assert sent_ops(tab, sent, "overview_set") == []
