import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

storage = core

SLUG = "demo-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "p", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    doc = new_ledger.build_doc(content)
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(doc, SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    storage.sync(SLUG)


def chat(state):
    return state["chat"]


class ChatOps(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_clear_empties_chat_and_logs_one_event(self):
        for n in range(3):
            storage.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": f"m-{n}", "text": "hi"}])
        state, rejected = storage.sync(SLUG, ops=[{"op": "clear", "thread": "chat", "id": "c-1"}])
        self.assertEqual(rejected, [])
        self.assertEqual(chat(state), [])
        self.assertEqual([e["kind"] for e in state["_meta"]["events"]].count("chat cleared"), 1)

    def test_clear_on_empty_chat_logs_nothing(self):
        before = storage.sync(SLUG)[0]["_meta"]["rev"]
        state, _ = storage.sync(SLUG, ops=[{"op": "clear", "thread": "chat", "id": "c-2"}])
        self.assertEqual(state["_meta"]["rev"], before)

    def test_clear_is_refused_outside_chat(self):
        _, rejected = storage.sync(SLUG, ops=[{"op": "clear", "thread": "notes", "id": "c-3"}])
        self.assertEqual(rejected, ["c-3"])

    def test_an_added_note_carries_an_empty_comments_thread(self):
        state, rejected = storage.sync(SLUG, ops=[{"op": "add", "thread": "notes", "id": "n-1", "text": "Later"}])
        self.assertEqual(rejected, [])
        self.assertEqual(state["notes"][-1]["comments"], [])

    def test_a_note_added_by_a_member_carries_an_empty_comments_thread(self):
        storage.sync(SLUG, ops=[{"op": "join", "id": "j-1", "by": "eng"}])
        state, rejected = storage.sync(
            SLUG, ops=[{"op": "add", "thread": "notes", "id": "n-2", "by": "eng", "text": "Later"}]
        )
        self.assertEqual(rejected, [])
        self.assertEqual(
            state["notes"][-1],
            {"id": "n-2", "by": "eng", "at": state["notes"][-1]["at"], "text": "Later", "comments": []},
        )

    def test_an_added_chat_line_has_no_comments_thread(self):
        state, _ = storage.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m-9", "text": "hi"}])
        self.assertEqual(state["chat"][-1]["id"], "m-9")
        self.assertNotIn("comments", state["chat"][-1])

    def test_check_body_accepts_clear_without_text(self):
        core.check_body({"ops": [{"op": "clear", "thread": "chat", "id": "c-4"}]})

    def test_page_version_tracks_the_template(self):
        self.assertRegex(core.page_version(), r"^[0-9a-f]{12}$")
        page = server.page_for(SLUG)
        self.assertEqual(core.PAGE_RE.search(page).group(1), core.page_version())


class Start(unittest.TestCase):
    def test_earliest_ignores_legacy_zero_and_keeps_the_oldest(self):
        meta = {"stamps": {"a": {"at": 500}, "b": {"at": 0}}, "events": [{"at": 900}, {"at": 0}], "created_at": 700}
        self.assertEqual(core.earliest(meta, 1000), 500)
        self.assertEqual(core.earliest({"stamps": {}, "events": []}, 1000), 1000)

    def test_sync_persists_created_at(self):
        make_ledger()
        meta = storage.sync(SLUG)[0]["_meta"]
        self.assertGreater(meta["created_at"], 0)
        self.assertNotIn("started_at", meta)


if __name__ == "__main__":
    unittest.main()
