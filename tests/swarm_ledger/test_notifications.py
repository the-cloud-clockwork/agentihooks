import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "notify-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": "boss", "role": "orchestrator"}])
    return f"phases/{state['phases'][0]['id']}"


def notes():
    state, _ = core.sync(SLUG)
    return state["notifications"]


def comment(n, phase, text, by=None):
    op = {"op": "add", "thread": f"{phase}/comments", "id": f"c-{n}", "text": text}
    return {**op, "by": by} if by else op


class Notifications(unittest.TestCase):
    def setUp(self):
        self.phase = make_ledger()

    def test_new_followup_and_question_from_an_agent_notify(self):
        core.sync(
            SLUG,
            ops=[
                {"op": "add_item", "id": "a1", "by": "boss", "list": "followups", "text": "Check the disk"},
                {"op": "add_item", "id": "a2", "by": "boss", "list": "questions", "text": "Which broker first?"},
            ],
        )
        rows = notes()
        self.assertEqual([r["label"] for r in rows], ["New follow-up", "New open question"])
        self.assertEqual([r["item"].split("/")[0] for r in rows], ["followups", "questions"])
        self.assertEqual(rows[1]["text"], "Which broker first?")
        self.assertTrue(all(isinstance(r["at"], int) and r["at"] > 0 for r in rows))

    def test_an_agent_answer_after_the_operator_notifies_once(self):
        core.sync(SLUG, ops=[comment(1, self.phase, "Is this blocked?")])
        core.sync(SLUG, ops=[comment(2, self.phase, "No, it waits on the review.", by="boss")])
        core.sync(SLUG, ops=[comment(3, self.phase, "The review is done now.", by="boss")])
        rows = notes()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["label"], rows[0]["item"], rows[0]["by"]), ("Reply", self.phase, "boss"))

    def test_a_chat_answer_after_the_operator_notifies(self):
        core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m1", "text": "where are we"}])
        core.sync(
            SLUG,
            ops=[
                {
                    "op": "add",
                    "thread": "chat",
                    "id": "m2",
                    "by": "boss",
                    "reply_to": "m1",
                    "text": "Phase one is half done.",
                }
            ],
        )
        self.assertEqual([(r["label"], r["item"]) for r in notes()], [("Reply", "chat")])

    def test_a_long_answer_is_kept_short_on_the_row(self):
        core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m1", "text": "expand please"}])
        long = " ".join(["The phase is moving along well."] * 40)
        core.sync(
            SLUG,
            ops=[
                {"op": "add", "thread": "chat", "id": "m2", "by": "boss", "long": True, "to": "operator", "text": long}
            ],
        )
        self.assertEqual(notes()[0]["text"], long[:280])

    def test_operator_actions_and_agent_status_alone_do_not_notify(self):
        core.sync(SLUG, ops=[comment(1, self.phase, "a note to self")])
        core.sync(SLUG, ops=[comment(2, self.phase, "and a second one")])
        core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m0", "text": "first ask"}])
        core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m00", "text": "second ask"}])
        self.assertEqual(notes(), [])

    def test_only_the_operator_clears_one_or_all(self):
        core.sync(
            SLUG,
            ops=[
                {"op": "add_item", "id": f"a{n}", "by": "boss", "list": "followups", "text": f"Follow up {n}"}
                for n in (1, 2, 3)
            ],
        )
        first = notes()[0]["id"]
        with self.assertRaises(ValueError):
            core.check_op({"op": "notification_clear", "id": "x", "by": "boss", "target": first})
        state, rejected = core.sync(SLUG, ops=[{"op": "notification_clear", "id": "x1", "target": first}])
        self.assertEqual((rejected, len(state["notifications"])), ([], 2))
        state, _ = core.sync(SLUG, ops=[{"op": "notification_clear", "id": "x2", "target": "all"}])
        self.assertEqual(state["notifications"], [])

    def test_no_agent_path_creates_a_notification(self):
        with self.assertRaises(ValueError):
            core.check_op({"op": "notification", "id": "n1", "by": "boss", "item": self.phase, "text": "hi"})
        html_path, _ = core.paths(SLUG)
        html = html_path.read_text(encoding="utf-8")
        forged = html.replace(
            '"notifications": []',
            '"notifications": [{"id": "f", "item": "chat", "label": "Reply", "text": "x", "by": "boss", "at": 1}]',
        )
        self.assertNotEqual(forged, html)
        html_path.write_text(forged, encoding="utf-8")
        self.assertEqual(notes(), [])

    def test_a_forged_seed_on_a_fresh_ledger_carries_no_notification(self):
        html_path, json_path = core.paths(SLUG)
        html = html_path.read_text(encoding="utf-8")
        row = '{"id": "f", "item": "chat", "label": "Reply", "text": "x", "by": "boss", "at": 1}'
        forged = html.replace('"notifications": []', f'"notifications": [{row}]')
        self.assertNotEqual(forged, html)
        html_path.write_text(forged, encoding="utf-8")
        json_path.unlink()
        self.assertEqual(notes(), [])
