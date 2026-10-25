import new_ledger

from tests.swarm_ledger.ledger_page import ledger_state, shell_html, show
from tests.swarm_ledger.test_comment_author_lines import browser as browser

ISSUE = "https://github.com/the-cloud-clockwork/agentihooks/issues/613"
NOTES = "http://example.com/file_name.py?run=37119097059&view=open#4a5414f78"
TEXT = (
    f"Read <b>the issue</b> at {ISSUE}, then {NOTES}. Local core/strategy/reentry.py and javascript:alert(1) stay text."
)


def test_chat_and_comments_render_web_links_safely_and_open_a_new_tab(browser):
    doc = new_ledger.build_doc(
        {
            "title": "Web links",
            "overview": "o",
            "phases": [{"title": "phase"}],
            "tasks": [{"title": "task"}],
            "questions": [{"text": "question"}],
            "followups": [{"text": "follow up"}],
        }
    )
    entry = {"id": "reply", "by": "engineer", "at": 1, "text": TEXT}
    doc["chat"] = [entry]
    for key in ("phases", "tasks", "questions", "followups"):
        doc[key][0]["comments"] = [entry]
    with browser.new_context() as context:
        context.route("https://github.com/**", lambda route: route.fulfill(body="Issue page"))
        tab = context.new_page()
        show(tab, shell_html(), ledger=ledger_state(doc))
        for key in ("phases", "tasks", "questions", "followups"):
            tab.locator(f"#sec-{key} button[data-comments]").click()
        tab.locator("#chat-fab").click()
        bodies = tab.locator(".entry-body")
        assert bodies.count() == 5
        for body in bodies.all():
            assert body.text_content() == TEXT
            assert body.locator("b, script").count() == 0
            anchors = body.locator("a")
            assert anchors.count() == 2
            for anchor, url in zip(anchors.all(), (ISSUE, NOTES), strict=True):
                assert anchor.text_content() == url
                assert anchor.get_attribute("href") == url
                assert anchor.get_attribute("target") == "_blank"
                assert set(anchor.get_attribute("rel").split()) >= {"noopener", "noreferrer"}
        with tab.expect_popup() as popup:
            tab.locator("#chat-log a").first.click()
        assert popup.value.url == ISSUE
