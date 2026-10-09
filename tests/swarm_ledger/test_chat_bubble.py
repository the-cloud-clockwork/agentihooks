import subprocess
import unittest
from pathlib import Path

from tests.swarm_ledger.ledger_page import page_source

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"


def function_source(name):
    page = page_source()
    return f"function {name}(" + page.split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


class ChatBubble(unittest.TestCase):
    def test_unread_counts_only_new_agent_messages(self):
        script = (
            function_source("unreadCount")
            + """
const assert = require("node:assert/strict");
const chat = [
  {id: "a", by: "operator", at: 50, text: "where are we"},
  {id: "b", by: "boss", at: 60, text: "half done"},
  {id: "c", by: "boss", at: 40, text: "old"},
  {id: "d", by: "boss", at: 70, text: "", deleted: true},
  {id: "e", by: "operator", at: 80, text: "thanks"},
  {id: "f", by: "boss", at: 90, text: "merged"},
];
assert.equal(unreadCount(chat, 45), 2);
assert.equal(unreadCount(chat, 90), 0);
assert.equal(unreadCount(chat, 0), 3);
assert.equal(unreadCount([], 0), 0);
"""
        )
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_the_chat_lives_in_a_floating_panel(self):
        page = page_source()
        side = page.split('id="swarm" role="tabpanel"', 1)[1].split("</main>", 1)[0]
        self.assertNotIn("chat-log", side)
        for marker in ('id="chat-fab"', 'id="chat-badge"', 'id="chat-panel"', 'id="chat-size"', 'id="chat-close"'):
            self.assertIn(marker, page)

    def test_a_hidden_badge_is_not_drawn(self):
        page = page_source()
        self.assertRegex(page, r"\.sync-badge\[hidden\][^{]*\{\s*display:\s*none;")

    def test_notification_text_is_a_wide_clickable_target(self):
        page = page_source()
        self.assertRegex(page, r"\.notif, \.chat-panel \{[^}]*width: min\(600px")
        self.assertRegex(page, r"\.notif-text:hover[^{]*\{[^}]*background")
        self.assertRegex(
            page,
            r'(?s)class: "notif-text".{0,240}?on: \{ click: \(ev\) => \{ if \(n\.item && !getSelection\(\)\.toString\(\)\) jumpToNotice\(ev, n\.item\)',
        )
