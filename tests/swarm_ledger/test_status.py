import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_comments as comments  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import ledger_link  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "status-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}, {"title": "two", "description": "d"}],
        "questions": [{"text": "which broker?"}],
        "followups": [{"text": "check disk"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    core.sync(
        SLUG,
        ops=[{"op": "join", "id": "j1", "by": "boss", "role": "orchestrator"}, {"op": "join", "id": "j2", "by": "eng"}],
    )
    return state


def post(by, text, n, thread, **extra):
    return {"op": "add", "thread": thread, "id": f"c-{by}-{n}", "text": text, "by": by, **extra}


def agent(kind, by, n, **fields):
    return {"op": kind, "id": f"{kind}-{by}-{n}", "by": by, **fields}


def live(entries):
    return [e for e in entries if not e.get("deleted")]


class Style(unittest.TestCase):
    def test_noise_is_refused_with_a_reason(self):
        for text in (
            "Merged at 19:30Z.",
            "Head 4a5414f78 is green.",
            "Run 37119097059 passed.",
            "Fixed in core/strategy/reentry.py.",
            "Done on 2026-10-03.",
            "Fixed engine_setup.py.",
        ):
            with self.assertRaises(ValueError, msg=text):
                comments.check(text, "comment")

    def test_plain_status_passes(self):
        comments.check("Fixed and merged in PR #3364; the planner now refuses a zero stop.", "comment")

    def test_word_limits(self):
        with self.assertRaises(ValueError):
            comments.check("word " * 51, "comment")
        comments.check("word " * 90, "chat")
        with self.assertRaises(ValueError):
            comments.check("word " * 101, "chat")
        comments.check("word " * 101, "chat", long=True)

    def test_a_ledger_link_whose_name_carries_a_date_passes(self):
        link = ledger_link.page_url("okay-we-re-going-to-mossy-rabin-2026-10-05")
        comments.check(f"The swarm is running, follow it at {link}", "chat")
        comments.check(f"Ledger page {link}/ is open.", "chat")

    def test_a_bare_date_next_to_a_ledger_link_is_refused(self):
        link = ledger_link.page_url("okay-we-re-going-to-mossy-rabin-2026-10-05")
        with self.assertRaises(ValueError):
            comments.check(f"Merged on 2026-10-05, see {link}", "chat")

    def test_a_web_link_can_carry_a_date(self):
        comments.check("Notes at http://example.com/notes-2026-10-05", "chat")

    def test_web_links_pass_and_noise_outside_them_stays_refused(self):
        for kind in comments.LIMITS:
            link = "https://github.com/the-cloud-clockwork/agentihooks/issues/613"
            comments.check(f"Read {link}", kind)
            comments.check("Read https://example.com/file_name.py?run=37119097059#4a5414f78", kind)
            for noise in ("core/strategy/reentry.py", "engine_setup", "37119097059", "4a5414f78"):
                with self.assertRaises(ValueError, msg=noise):
                    comments.check(f"Read {link} then {noise}", kind)

    def test_chat_and_comment_writes_accept_issue_links(self):
        make_ledger()
        text = "The issue is https://github.com/the-cloud-clockwork/agentihooks/issues/613"
        ops = [post("eng", text, 1, "chat", to="operator"), post("eng", text, 2, "phases/p1/comments")]
        core.check_body({"ops": ops})
        state, _ = core.sync(SLUG, ops=ops)
        self.assertEqual([entry["text"] for entry in state["chat"]], [text])
        self.assertEqual(state["phases"][0]["comments"][0]["text"], text)


class Comments(unittest.TestCase):
    def setUp(self):
        state = make_ledger()
        self.item = f"phases/{state['phases'][0]['id']}"
        self.thread = f"{self.item}/comments"

    def entries(self):
        return live(core.sync(SLUG)[0]["phases"][0]["comments"])

    def test_a_second_comment_amends_the_first(self):
        core.sync(SLUG, ops=[post("eng", "Started the fix.", 1, self.thread)])
        core.sync(SLUG, ops=[post("eng", "Fixed and merged.", 2, self.thread)])
        entries = self.entries()
        self.assertEqual([(e["by"], e["text"]) for e in entries], [("eng", "Fixed and merged.")])
        self.assertIn("edited_at", entries[0])

    def test_an_operator_comment_starts_a_new_reply(self):
        core.sync(SLUG, ops=[post("eng", "Started the fix.", 1, self.thread)])
        core.sync(SLUG, ops=[{"op": "add", "thread": self.thread, "id": "op-1", "text": "why so slow?"}])
        core.sync(SLUG, ops=[post("eng", "Waiting on review.", 2, self.thread)])
        self.assertEqual([e["by"] for e in self.entries()], ["eng", "operator", "eng"])

    def test_each_agent_keeps_its_own_comment(self):
        core.sync(SLUG, ops=[post("eng", "Started.", 1, self.thread), post("boss", "Assigned to eng.", 1, self.thread)])
        self.assertEqual([e["by"] for e in self.entries()], ["eng", "boss"])

    def test_a_noisy_comment_is_refused(self):
        with self.assertRaises(ValueError):
            core.check_body({"ops": [post("eng", "Merged 4a5414f78 at 19:30Z.", 1, self.thread)]})

    def test_phase_status_amends_the_comment(self):
        core.sync(SLUG, ops=[post("eng", "Started the fix.", 1, self.thread)])
        core.sync(SLUG, ops=[agent("set", "eng", 1, path=f"{self.item}/done", value=True, status="Fixed and merged.")])
        self.assertEqual([e["text"] for e in self.entries()], ["Fixed and merged."])

    def test_noisy_phase_status_is_refused(self):
        with self.assertRaises(ValueError):
            core.check_body(
                {"ops": [agent("set", "eng", 1, path=f"{self.item}/done", value=True, status="run 37119097059")]}
            )

    def test_noisy_chat_is_refused_and_long_is_chat_only(self):
        with self.assertRaises(ValueError):
            core.check_body({"ops": [post("boss", "Checkpoint 08:28Z.", 1, "chat")]})
        core.check_body({"ops": [post("boss", "word " * 120, 2, "chat", long=True)]})
        with self.assertRaises(ValueError):
            core.check_body({"ops": [post("boss", "Plain.", 3, self.thread, long=True)]})


class EditDelete(unittest.TestCase):
    def setUp(self):
        state = make_ledger()
        self.thread = f"phases/{state['phases'][0]['id']}/comments"

    def entries(self):
        return core.sync(SLUG)[0]["phases"][0]["comments"]

    def change(self, kind, by, entry_id, text=None):
        op = {"op": kind, "thread": self.thread, "id": entry_id, "by": by}
        if text is not None:
            op["text"] = text
        return core.sync(SLUG, ops=[op])[1]

    def test_an_agent_edits_and_deletes_its_own_entry(self):
        core.sync(SLUG, ops=[post("eng", "Started.", 1, self.thread)])
        self.assertEqual(self.change("edit", "eng", "c-eng-1", "Fixed."), [])
        self.assertEqual(self.entries()[0]["text"], "Fixed.")
        self.assertEqual(self.change("delete", "eng", "c-eng-1"), [])
        self.assertTrue(self.entries()[0]["deleted"])

    def test_a_member_cannot_touch_another_agents_entry(self):
        core.sync(SLUG, ops=[post("boss", "Assigned.", 1, self.thread)])
        self.assertEqual(self.change("delete", "eng", "c-boss-1"), ["c-boss-1"])

    def test_the_orchestrator_cleans_any_agent_entry(self):
        core.sync(SLUG, ops=[post("eng", "Started.", 1, self.thread)])
        self.assertEqual(self.change("delete", "boss", "c-eng-1"), [])
        self.assertTrue(self.entries()[0]["deleted"])

    def test_operator_entries_are_never_touched(self):
        core.sync(SLUG, ops=[{"op": "add", "thread": self.thread, "id": "op-1", "text": "why?"}])
        self.assertEqual(self.change("delete", "boss", "op-1"), ["op-1"])
        self.assertEqual(self.change("edit", "boss", "op-1", "because"), ["op-1"])

    def test_an_agent_edit_is_checked_for_noise(self):
        with self.assertRaises(ValueError):
            core.check_body(
                {"ops": [{"op": "edit", "thread": self.thread, "id": "x", "text": "at 19:30Z", "by": "eng"}]}
            )


class Scope(unittest.TestCase):
    def setUp(self):
        self.state = make_ledger()
        self.phase = f"phases/{self.state['phases'][0]['id']}"

    def first(self, name="phases"):
        return core.sync(SLUG)[0][name][0]

    def test_out_of_scope_clears_done(self):
        core.sync(SLUG, ops=[agent("set", "boss", 1, path=f"{self.phase}/done", value=True)])
        core.sync(SLUG, ops=[agent("set", "boss", 2, path=f"{self.phase}/out_of_scope", value=True)])
        item = self.first()
        self.assertEqual((item["done"], item["out_of_scope"]), (False, True))

    def test_done_clears_out_of_scope_for_agent_and_operator(self):
        core.sync(SLUG, ops=[agent("set", "boss", 1, path=f"{self.phase}/out_of_scope", value=True)])
        core.sync(SLUG, ops=[agent("set", "boss", 2, path=f"{self.phase}/done", value=True)])
        self.assertEqual((self.first()["done"], self.first()["out_of_scope"]), (True, False))
        core.sync(SLUG, ops=[agent("set", "boss", 3, path=f"{self.phase}/out_of_scope", value=True)])
        core.sync(SLUG, changes=[{"path": f"{self.phase}/done", "value": True, "base": False}])
        self.assertEqual((self.first()["done"], self.first()["out_of_scope"]), (True, False))

    def test_a_question_can_be_out_of_scope(self):
        qid = self.state["questions"][0]["id"]
        _, rejected = core.sync(SLUG, ops=[agent("set", "boss", 1, path=f"questions/{qid}/out_of_scope", value=True)])
        self.assertEqual(rejected, [])
        self.assertTrue(self.first("questions")["out_of_scope"])

    def test_out_of_scope_counts_as_resolved_for_the_gate(self):
        doc = {"phases": [{"done": True}, {"out_of_scope": True}], "followups": [{"out_of_scope": True}]}
        self.assertTrue(gate.closed(doc))
        self.assertFalse(gate.closed({"phases": [{"done": False}], "followups": []}))


class Seed(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def seed_comment(self, entry):
        html_path = core.paths(SLUG)[0]
        html = html_path.read_text(encoding="utf-8")
        seed = core.loads(core.SEED_RE.search(html).group(2))
        rev = seed.pop("_rev")
        seed["phases"][0]["comments"].append(entry)
        html_path.write_text(
            core.SEED_RE.sub(lambda m: m.group(1) + core.seed_text(seed, rev) + m.group(3), html, count=1),
            encoding="utf-8",
        )
        return core.sync(SLUG)[0]

    def test_seed_comments_amend_and_noise_becomes_a_warning(self):
        self.seed_comment({"id": "eng-1", "by": "eng", "text": "Started."})
        state = self.seed_comment({"id": "eng-2", "by": "eng", "text": "Fixed."})
        self.assertEqual([e["text"] for e in live(state["phases"][0]["comments"])], ["Fixed."])
        state = self.seed_comment({"id": "eng-3", "by": "eng", "text": "Head 4a5414f78 green."})
        self.assertEqual([e["text"] for e in live(state["phases"][0]["comments"])], ["Fixed."])
        self.assertTrue(any("refused" in w for w in state["_meta"]["warnings"]), json.dumps(state["_meta"]["warnings"]))


class OperatorScope(unittest.TestCase):
    def setUp(self):
        state = make_ledger()
        self.path = f"followups/{state['followups'][0]['id']}/out_of_scope"

    def test_unticking_the_dot_brings_the_item_back_with_a_comment(self):
        core.sync(SLUG, changes=[{"path": self.path, "value": True, "base": False}])
        state, rejected = core.sync(SLUG, changes=[{"path": self.path, "value": False, "base": True}])
        self.assertEqual(rejected, [])
        item = state["followups"][0]
        self.assertFalse(item["out_of_scope"])
        self.assertEqual(
            [(e["by"], e["text"]) for e in item["comments"]],
            [("operator", "Out of scope."), ("operator", "Back in scope.")],
        )
        kinds = [(e["by"], e["kind"]) for e in state["_meta"]["events"]]
        self.assertIn(("operator", "back in scope"), kinds)


class ItemText(unittest.TestCase):
    def setUp(self):
        self.state = make_ledger()
        self.item = f"followups/{self.state['followups'][0]['id']}"

    def test_retext_rewrites_a_followup(self):
        _, rejected = core.sync(
            SLUG, ops=[agent("retext", "boss", 1, item=self.item, text="Check the disk is not full.")]
        )
        self.assertEqual(rejected, [])
        self.assertEqual(core.sync(SLUG)[0]["followups"][0]["text"], "Check the disk is not full.")

    def test_noisy_item_text_is_refused(self):
        with self.assertRaises(ValueError):
            core.check_body(
                {"ops": [agent("retext", "boss", 1, item=self.item, text="Fix rows 767 (a) and (b) in units.py")]}
            )
        with self.assertRaises(ValueError):
            core.check_body(
                {"ops": [agent("add_item", "boss", 2, list="followups", text="FOLLOW UP: run 37119097059")]}
            )


class Audit(unittest.TestCase):
    def test_audit_lists_noise_and_extra_comments(self):
        doc = {
            "phases": [
                {
                    "id": "p1",
                    "title": "t",
                    "comments": [
                        {"id": "a", "by": "eng", "text": "Merged 4a5414f78."},
                        {"id": "b", "by": "eng", "text": "Done."},
                        {"id": "c", "by": "operator", "text": "ok 19:30Z"},
                    ],
                }
            ],
            "questions": [],
            "followups": [{"id": "f1", "text": "OUT OF SCOPE: x", "comments": []}],
            "chat": [
                {"id": "m", "by": "boss", "text": "Checkpoint 08:28Z."},
                {"id": "n", "by": "boss", "text": "All good."},
            ],
        }
        rows = {(where, entry_id) for where, entry_id, _, _ in comments.audit(doc)}
        self.assertEqual(rows, {("phases/p1", "a"), ("phases/p1", "-"), ("followups/f1", "text"), ("chat", "m")})


class FollowupReopen(unittest.TestCase):
    def test_the_cli_reopens_a_followup(self):
        import ledger

        args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "followup", "open", "f1"])
        self.assertEqual((args.action, args.value), ("open", "f1"))

    def test_an_agent_unchecks_a_done_followup(self):
        state = make_ledger()
        path = f"followups/{state['followups'][0]['id']}/done"
        core.sync(SLUG, ops=[agent("set", "boss", 1, path=path, value=True)])
        state, rejected = core.sync(SLUG, ops=[agent("set", "boss", 2, path=path, value=False)])
        self.assertEqual(rejected, [])
        self.assertFalse(state["followups"][0]["done"])
        self.assertIn(("boss", "unchecked"), [(e["by"], e["kind"]) for e in state["_meta"]["events"]])


if __name__ == "__main__":
    unittest.main()
