import pytest

from tests.swarm_ledger import test_freeze_page
from tests.swarm_ledger.test_freeze_page import browser, page, sent_ops  # noqa: F401


def words(count):
    return " ".join(f"word{n}" for n in range(count))


@pytest.fixture
def overview(request, monkeypatch):
    monkeypatch.setitem(test_freeze_page.DOC, "overview", request.param)
    return request.param


@pytest.mark.parametrize("overview", [words(300)], indirect=True, ids=["300 words"])
def test_a_long_overview_shows_its_first_two_hundred_words_and_an_ellipsis(overview, page):  # noqa: F811
    tab, _ = page
    assert tab.locator("#overview").text_content() == words(200) + "…"
    tab.click("#overview-edit")
    assert tab.locator("#overview-input").input_value() == overview


@pytest.mark.parametrize(
    "overview", [words(199), "Short\n\noverview  kept as written"], indirect=True, ids=["199 words", "spacing"]
)
def test_an_overview_under_two_hundred_words_shows_unchanged(overview, page):  # noqa: F811
    tab, _ = page
    assert tab.locator("#overview").text_content() == overview


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
