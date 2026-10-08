import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "crew-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}, {"title": "two", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


def agent(kind, by, **fields):
    return {
        "op": kind,
        "id": f"{kind}-{by}-{len(fields)}-{fields.get('rev', '')}-{fields.get('item', '')}",
        "by": by,
        **fields,
    }


def operator_chat(text, n):
    return {"op": "add", "thread": "chat", "id": f"op-{n}", "text": text}


class Crew(unittest.TestCase):
    def setUp(self):
        make_ledger()
        core.sync(SLUG, ops=[agent("join", "boss", role="orchestrator"), agent("join", "eng")])

    def meta(self):
        return core.sync(SLUG)[0]["_meta"]

    def test_join_registers_members_and_history_is_not_owed(self):
        meta = self.meta()
        self.assertEqual(set(meta["members"]), {"boss", "eng"})
        self.assertEqual(gate.unhandled_for(meta, "boss"), [])

    def test_chat_goes_to_the_orchestrator_unless_mentioned(self):
        core.sync(SLUG, ops=[operator_chat("status?", 1), operator_chat("@eng how is it", 2)])
        meta = self.meta()
        self.assertEqual([e["text"] for e in gate.unhandled_for(meta, "boss")], ["status?"])
        self.assertEqual([e["text"] for e in gate.unhandled_for(meta, "eng")], ["@eng how is it"])

    def test_item_events_follow_the_claim(self):
        phase_id = core.sync(SLUG)[0]["phases"][0]["id"]
        core.sync(SLUG, ops=[agent("claim", "eng", item=f"phases/{phase_id}")])
        core.sync(SLUG, ops=[{"op": "add", "thread": f"phases/{phase_id}/comments", "id": "oc-1", "text": "look"}])
        meta = self.meta()
        self.assertEqual(len(gate.unhandled_for(meta, "eng")), 1)
        self.assertEqual(gate.unhandled_for(meta, "boss"), [])

    def test_ack_clears_what_was_owed(self):
        core.sync(SLUG, ops=[operator_chat("ping", 3)])
        rev = self.meta()["rev"]
        core.sync(SLUG, ops=[agent("ack", "boss", rev=rev)])
        self.assertEqual(gate.unhandled_for(self.meta(), "boss"), [])

    def test_phase_set_is_attributed_to_the_agent(self):
        phase_id = core.sync(SLUG)[0]["phases"][0]["id"]
        state, rejected = core.sync(
            SLUG, ops=[agent("set", "eng", path=f"phases/{phase_id}/done", value=True, status="merged")]
        )
        self.assertEqual(rejected, [])
        self.assertTrue(state["phases"][0]["done"])
        kinds = [(e["by"], e["kind"]) for e in state["_meta"]["events"]]
        self.assertIn(("eng", "checked"), kinds)
        self.assertEqual(state["phases"][0]["comments"][-1]["by"], "eng")

    def test_agent_ops_cannot_be_the_operator(self):
        with self.assertRaises(ValueError):
            core.check_body({"ops": [agent("join", "operator")]})
        with self.assertRaises(ValueError):
            core.check_body({"ops": [{"op": "add", "thread": "notes", "id": "n1", "text": "x", "by": "eng"}]})

    def test_followup_add_and_close(self):
        state, _ = core.sync(SLUG, ops=[agent("add_item", "eng", list="followups", text="check disk")])
        item = state["followups"][-1]
        self.assertEqual(item["text"], "check disk")
        state, _ = core.sync(SLUG, ops=[agent("set", "eng", path=f"followups/{item['id']}/done", value=True)])
        self.assertTrue(state["followups"][-1]["done"])

    def test_crew_status_counts_unhandled(self):
        core.sync(SLUG, ops=[operator_chat("one", 4), operator_chat("two", 5)])
        crew = {m["name"]: m for m in gate.crew(self.meta())}
        self.assertEqual(crew["boss"]["unhandled"], 2)
        self.assertEqual(crew["eng"]["unhandled"], 0)


if __name__ == "__main__":
    unittest.main()
