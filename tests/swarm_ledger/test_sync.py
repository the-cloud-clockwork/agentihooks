import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger.repository import repository as storage  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "sync-2026-01-01"


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
    storage.apply_ops(SLUG)
    storage.apply_ops(
        SLUG,
        ops=[{"op": "join", "id": "j1", "by": "boss", "role": "orchestrator"}, {"op": "join", "id": "j2", "by": "eng"}],
    )


class SyncRequest(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def sync_event(self, state):
        return [e for e in state["_meta"]["events"] if e["kind"] == "sync requested"]

    def test_the_button_records_one_sync_event_with_a_summary(self):
        core.check_body({"ops": [{"op": "sync", "id": "s1"}]})
        storage.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m1", "text": "status?"}])
        state, rejected = storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "s1"}])
        self.assertEqual(rejected, [])
        events = self.sync_event(state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["by"], "operator")
        text = events[0]["text"]
        self.assertIn("1 operator message", text)
        self.assertIn("1 phase open", text)
        self.assertIn("1 follow-up open", text)
        self.assertIn("ack", text)

    def test_every_member_owes_the_sync_until_it_acks(self):
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "s2"}])
        meta = state["_meta"]
        for name in ("boss", "eng"):
            self.assertEqual([e["kind"] for e in gate.unhandled_for(meta, name)], ["sync requested"], name)
        storage.apply_ops(SLUG, ops=[{"op": "ack", "id": "a1", "by": "eng", "rev": meta["rev"]}])
        meta = storage.apply_ops(SLUG)[0]["_meta"]
        self.assertEqual(gate.unhandled_for(meta, "eng"), [])
        self.assertEqual(len(gate.unhandled_for(meta, "boss")), 1)

    def test_sync_takes_no_thread_and_no_author(self):
        with self.assertRaises(ValueError):
            core.check_body({"ops": [{"op": "sync", "id": "s3", "by": "eng"}]})


class Cooldown(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_a_second_sync_within_five_minutes_is_refused(self):
        storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "c1"}])
        state, rejected = storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "c2"}])
        self.assertEqual(rejected, ["c2"])
        self.assertEqual(len([e for e in state["_meta"]["events"] if e["kind"] == "sync requested"]), 1)

    def test_a_sync_after_the_cooldown_is_recorded(self):
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "c3"}])
        for event in state["_meta"]["events"]:
            if event["kind"] == "sync requested":
                event["at"] -= core.SYNC_COOLDOWN_MS + 1000
        storage.import_document(SLUG, state, replace=True)
        state, rejected = storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "c4"}])
        self.assertEqual(rejected, [])
        self.assertEqual(len([e for e in state["_meta"]["events"] if e["kind"] == "sync requested"]), 2)


class StatsSync(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_a_stats_sync_is_owed_by_the_orchestrator_only(self):
        core.check_body({"ops": [{"op": "stats_sync", "id": "t1"}]})
        state, rejected = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t1"}])
        self.assertEqual(rejected, [])
        event = [e for e in state["_meta"]["events"] if e["kind"] == "stats sync requested"][0]
        self.assertTrue(event["text"].startswith("Operator stats check, computed now. "))
        self.assertIn("Undecided follow-ups: check disk. ", event["text"])
        self.assertIn("Time left: the page shows ", event["text"])
        self.assertEqual(len(gate.unhandled_for(state["_meta"], "boss")), 1)
        self.assertEqual(gate.unhandled_for(state["_meta"], "eng"), [])

    def test_each_sync_kind_has_its_own_cooldown(self):
        storage.apply_ops(SLUG, ops=[{"op": "sync", "id": "t2"}])
        _, rejected = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t3"}])
        self.assertEqual(rejected, [])
        _, rejected = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t4"}])
        self.assertEqual(rejected, ["t4"])

    def answers(self, state):
        return [e for e in state["_meta"]["events"] if e["kind"] == "stats check answered"]

    def test_the_orchestrators_ack_answers_the_stats_check_once(self):
        rev = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t5"}])[0]["_meta"]["rev"]
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "ack", "id": "a2", "by": "boss", "rev": rev}])
        self.assertEqual(
            [(e["by"], e["id"], e["at"]) for e in self.answers(state)], [("boss", "t5", state["_meta"]["updated_at"])]
        )
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "ack", "id": "a3", "by": "boss", "rev": state["_meta"]["rev"]}])
        self.assertEqual(len(self.answers(state)), 1)

    def test_a_members_ack_or_an_older_ack_leaves_the_stats_check_sent(self):
        rev = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t6"}])[0]["_meta"]["rev"]
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "ack", "id": "a4", "by": "eng", "rev": rev}])
        self.assertEqual(self.answers(state), [])
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "ack", "id": "a5", "by": "boss", "rev": rev - 1}])
        self.assertEqual(self.answers(state), [])

    def test_an_ack_answers_only_the_latest_stats_check(self):
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t7"}])
        for event in state["_meta"]["events"]:
            if event["kind"] == "stats sync requested":
                event["at"] -= core.SYNC_COOLDOWN_MS + 1000
        storage.import_document(SLUG, state, replace=True)
        rev = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "t8"}])[0]["_meta"]["rev"]
        state, _ = storage.apply_ops(SLUG, ops=[{"op": "ack", "id": "a6", "by": "boss", "rev": rev}])
        self.assertEqual([e["id"] for e in self.answers(state)], ["t8"])

    def test_refresh_is_persisted_before_reply_while_master_remains_busy(self):
        state, _ = storage.apply_ops(SLUG)
        state["tasks"] = [
            {"id": "closed", "state": "done", "done": True, "comments": []},
            {"id": "remaining", "state": "open", "done": False, "comments": []},
        ]
        state["time_left_minutes"] = 400
        state["_meta"]["time_left"] = {"inputs": {"slots": 1, "ci_minutes": 35}}
        now = core.now_ms()
        state["_meta"]["events"].append({"kind": "task done", "target": "tasks/closed", "at": now})
        storage.import_document(SLUG, state, replace=True)
        reply, rejected = storage.apply_ops(SLUG, ops=[{"op": "stats_sync", "id": "persist"}])
        self.assertEqual(rejected, [])
        self.assertEqual(reply["time_left_minutes"], 60)
        persisted = storage.get_document(SLUG)
        self.assertEqual(persisted["_meta"]["stats_refresh"], reply["_meta"]["stats_refresh"])
        self.assertEqual(persisted["time_left_minutes"], 60)
        self.assertEqual(reply["_meta"]["stats_refresh"]["state"], "refreshed")
        self.assertEqual(len(gate.unhandled_for(reply["_meta"], "boss")), 1)

    def test_refresh_uses_changes_reconciled_in_the_latest_transaction(self):
        state, _ = storage.apply_ops(SLUG)
        followup = state["followups"][0]
        reply, rejected = storage.apply_ops(
            SLUG,
            changes=[{"path": f"followups/{followup['id']}/done", "value": True, "base": False}],
            ops=[{"op": "stats_sync", "id": "latest"}],
        )
        self.assertEqual(rejected, [])
        self.assertEqual(reply["_meta"]["stats_refresh"]["counts"]["followups"], {"done": 1, "total": 1})

    def test_refresh_runs_after_later_task_mutations_in_the_same_batch(self):
        reply, rejected = storage.apply_ops(
            SLUG,
            ops=[
                {"op": "stats_sync", "id": "batch"},
                {"op": "task_add", "id": "new", "by": "boss", "task": "t1", "title": "New work", "lane": "eng"},
            ],
        )
        self.assertEqual(rejected, [])
        self.assertEqual(reply["_meta"]["stats_refresh"]["counts"]["tasks"], {"done": 0, "total": 1})
        self.assertEqual(reply["_meta"]["stats_refresh"]["calculation"]["remaining"], 1)
        self.assertIsNone(reply["_meta"]["stats_refresh"]["calculation"]["minutes"])


if __name__ == "__main__":
    unittest.main()
