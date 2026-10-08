import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "prio-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [{"text": "which?"}],
        "followups": [{"text": "check disk"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": "boss", "role": "orchestrator"}])
    return state


def add(n, item, text, by="boss"):
    return {"op": "priority", "id": f"pr-{n}", "by": by, "item": item, "text": text}


class Priorities(unittest.TestCase):
    def setUp(self):
        state = make_ledger()
        self.phase = f"phases/{state['phases'][0]['id']}"
        self.question = f"questions/{state['questions'][0]['id']}"

    def test_an_agent_adds_a_priority_with_an_event(self):
        state, rejected = core.sync(SLUG, ops=[add(1, self.phase, "Approve the broker choice so it can merge.")])
        self.assertEqual(rejected, [])
        self.assertEqual(
            [(p["item"], p["text"]) for p in state["priorities"] if not p.get("derived")],
            [(self.phase, "Approve the broker choice so it can merge.")],
        )
        self.assertIn(("boss", "priority added"), [(e["by"], e["kind"]) for e in state["_meta"]["events"]])

    def test_noise_long_text_and_operator_adds_are_refused(self):
        for op in (
            add(2, self.phase, "Merged 4a5414f78 at 19:30Z."),
            add(3, self.phase, "word " * 21),
            {"op": "priority", "id": "pr-4", "item": self.phase, "text": "Plain."},
        ):
            with self.assertRaises(ValueError):
                core.check_body({"ops": [op]})

    def test_an_unknown_item_is_rejected(self):
        _, rejected = core.sync(SLUG, ops=[add(5, "phases/nope", "Pick one.")])
        self.assertEqual(rejected, ["pr-5"])

    def test_one_priority_per_item(self):
        core.sync(SLUG, ops=[add(6, self.phase, "First ask.")])
        state, _ = core.sync(SLUG, ops=[add(7, self.phase, "Second ask.")])
        self.assertEqual([p["text"] for p in state["priorities"] if p["item"] == self.phase], ["Second ask."])

    def test_clear_by_id_and_all_from_operator_or_agent(self):
        core.sync(SLUG, ops=[add(8, self.phase, "Ask one."), add(9, self.question, "Ask two.")])
        state, rejected = core.sync(SLUG, ops=[{"op": "priority_clear", "id": "c1", "target": "pr-8"}])
        self.assertEqual(rejected, [])
        self.assertEqual([p["id"] for p in state["priorities"]], ["pr-9"])
        state, _ = core.sync(SLUG, ops=[{"op": "priority_clear", "id": "c2", "target": "all", "by": "boss"}])
        self.assertEqual(state["priorities"], [])
        cleared = [(e["by"], e["id"]) for e in state["_meta"]["events"] if e["kind"] == "priority cleared"]
        self.assertEqual(cleared, [("operator", "pr-8"), ("boss", "pr-9")])


if __name__ == "__main__":
    unittest.main()
