import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

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
    html_path.write_text(new_ledger.render(doc, SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


def chat(state):
    return state["chat"]


class ChatOps(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_clear_empties_chat_and_logs_one_event(self):
        for n in range(3):
            core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": f"m-{n}", "text": "hi"}])
        state, rejected = core.sync(SLUG, ops=[{"op": "clear", "thread": "chat", "id": "c-1"}])
        self.assertEqual(rejected, [])
        self.assertEqual(chat(state), [])
        self.assertEqual([e["kind"] for e in state["_meta"]["events"]].count("chat cleared"), 1)

    def test_clear_on_empty_chat_logs_nothing(self):
        before = core.sync(SLUG)[0]["_meta"]["rev"]
        state, _ = core.sync(SLUG, ops=[{"op": "clear", "thread": "chat", "id": "c-2"}])
        self.assertEqual(state["_meta"]["rev"], before)

    def test_clear_is_refused_outside_chat(self):
        _, rejected = core.sync(SLUG, ops=[{"op": "clear", "thread": "notes", "id": "c-3"}])
        self.assertEqual(rejected, ["c-3"])

    def test_check_body_accepts_clear_without_text(self):
        core.check_body({"ops": [{"op": "clear", "thread": "chat", "id": "c-4"}]})

    def test_page_version_tracks_the_template(self):
        self.assertRegex(core.page_version(), r"^[0-9a-f]{12}$")
        html = core.paths(SLUG)[0].read_text(encoding="utf-8")
        self.assertEqual(core.PAGE_RE.search(html).group(1), core.page_version())


class Start(unittest.TestCase):
    def test_earliest_ignores_legacy_zero_and_keeps_the_oldest(self):
        meta = {"stamps": {"a": {"at": 500}, "b": {"at": 0}}, "events": [{"at": 900}, {"at": 0}], "created_at": 700}
        self.assertEqual(core.earliest(meta, 1000), 500)
        self.assertEqual(core.earliest({"stamps": {}, "events": []}, 1000), 1000)

    def test_sync_persists_created_at(self):
        make_ledger()
        meta = core.sync(SLUG)[0]["_meta"]
        self.assertGreater(meta["created_at"], 0)
        self.assertNotIn("started_at", meta)


if __name__ == "__main__":
    unittest.main()
