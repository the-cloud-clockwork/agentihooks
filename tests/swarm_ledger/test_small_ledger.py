import contextlib
import io
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402

from scripts.swarm_ledger import ledger_bin, new_ledger  # noqa: E402
from scripts.swarm_ledger.repository import bin_storage, repository
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import browser_home

DAY_MS = 24 * 60 * 60 * 1000
T0 = 1_800_000_000_000
CONTENT = {
    "title": "Small work",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "look", "description": "d"}, {"title": "fix", "description": "d"}],
    "questions": [],
    "followups": [],
}


def at(ms):
    return unittest.mock.patch.object(core, "now_ms", return_value=ms)


def create(slug, *argv, env=None):
    path = core.LEDGER_DIR / f".content-{slug}.json"
    path.write_text(json.dumps(CONTENT), encoding="utf-8")
    out = io.StringIO()
    argv = ["agentihooks ledger new", "--content", str(path), *argv]
    with (
        unittest.mock.patch.object(new_ledger, "built_slug", lambda args: slug),
        unittest.mock.patch.object(sys, "argv", argv),
        unittest.mock.patch.dict(os.environ, env or {}),
        contextlib.redirect_stdout(out),
    ):
        new_ledger.main()
    return json.loads(out.getvalue().splitlines()[0])


def state(slug):
    return repository.get_document(slug)


def finish(slug):
    ops = [
        {"op": "set", "id": f"done-{p['id']}", "by": "w", "path": f"phases/{p['id']}/done", "value": True}
        for p in state(slug)["phases"]
    ]
    assert core.sync(slug, ops=ops)[1] == []


class SizeOnCreate(unittest.TestCase):
    def test_a_ledger_is_small_by_default_and_its_creator_joins_as_worker(self):
        with at(T0):
            out = create("size-small", "--as", "worker-1")
        self.assertEqual((out["size"], out["joined"]), ("small", "worker-1"))
        found = state("size-small")
        self.assertEqual(found["size"], "small")
        self.assertEqual(found["_meta"]["members"]["worker-1"]["role"], "member")

    def test_the_creator_name_falls_back_to_the_agent_name(self):
        with at(T0):
            out = create("size-env", env={"AGENTIHOOKS_AGENT_NAME": "env-worker"})
        self.assertEqual(out["joined"], "env-worker")
        self.assertIn("env-worker", state("size-env")["_meta"]["members"])

    def test_a_small_ledger_without_a_worker_name_is_refused_and_not_written(self):
        with unittest.mock.patch.dict(os.environ), self.assertRaises(SystemExit) as refused:
            os.environ.pop("AGENTIHOOKS_AGENT_NAME", None)
            create("size-nameless")
        self.assertIn("--as", str(refused.exception.code))
        self.assertFalse(repository.exists("size-nameless"))

    def test_a_swarm_ledger_needs_no_worker(self):
        with at(T0):
            out = create("size-swarm", "--size", "swarm")
        self.assertEqual(out["size"], "swarm")
        self.assertNotIn("joined", out)
        self.assertEqual(state("size-swarm")["size"], "swarm")
        self.assertEqual(state("size-swarm")["_meta"]["members"], {})

    def test_size_set_turns_a_small_ledger_into_a_swarm_ledger(self):
        with at(T0):
            create("size-grow", "--as", "w")
        core.sync("size-grow", ops=[{"op": "size_set", "id": "s1", "by": "swarm", "size": "swarm"}])
        self.assertEqual(state("size-grow")["size"], "swarm")
        with self.assertRaises(ValueError):
            core.check_body({"ops": [{"op": "size_set", "id": "s2", "by": "swarm", "size": "huge"}]})

    def test_home_shows_each_ledger_size_and_an_unsized_ledger_counts_as_swarm(self):
        with at(T0):
            create("home-small", "--as", "w")
            create("home-swarm", "--size", "swarm")
        html_path = core.paths("home-legacy")[0]
        html_path.write_text(legacy_page.render(new_ledger.build_doc(CONTENT, size=None), "home-legacy", 8765))
        core.sync("home-legacy")
        sizes = {s["slug"]: s["size"] for s in server.ledger_summaries()}
        self.assertEqual((sizes["home-small"], sizes["home-swarm"], sizes["home-legacy"]), ("small", "swarm", "swarm"))
        home = browser_home(server)
        self.assertIn('<span class="kind">small</span>', home)
        self.assertIn('<span class="kind">swarm</span>', home)


class Lifecycle(unittest.TestCase):
    def test_a_small_ledger_with_every_item_done_moves_to_the_bin(self):
        with at(T0):
            create("life-done", "--as", "w")
            finish("life-done")
        self.assertIn("life-done", bin_storage.auto_bin(now=T0 + 1000))
        self.assertEqual(ledger_bin.entries()["life-done"], T0 + 1000)
        self.assertNotIn("life-done", {s["slug"] for s in server.ledger_summaries()})

    def test_a_small_ledger_with_open_items_stays_until_seven_idle_days_pass(self):
        with at(T0):
            create("life-idle", "--as", "w")
        self.assertNotIn("life-idle", bin_storage.auto_bin(now=T0 + 7 * DAY_MS))
        self.assertIn("life-idle", bin_storage.auto_bin(now=T0 + 7 * DAY_MS + 1))

    def test_any_change_restarts_the_idle_clock(self):
        with at(T0):
            create("life-touched", "--as", "w")
        with at(T0 + 5 * DAY_MS):
            core.sync("life-touched", ops=[{"op": "add", "thread": "chat", "id": "c1", "text": "still here"}])
        self.assertNotIn("life-touched", bin_storage.auto_bin(now=T0 + 8 * DAY_MS))
        self.assertIn("life-touched", bin_storage.auto_bin(now=T0 + 12 * DAY_MS + 1))

    def test_swarm_and_unsized_ledgers_never_auto_bin(self):
        with at(T0):
            create("life-swarm", "--size", "swarm")
            finish("life-swarm")
            html_path = core.paths("life-legacy")[0]
            html_path.write_text(legacy_page.render(new_ledger.build_doc(CONTENT, size=None), "life-legacy", 8765))
            core.sync("life-legacy")
        binned = bin_storage.auto_bin(now=T0 + 400 * DAY_MS)
        self.assertNotIn("life-swarm", binned)
        self.assertNotIn("life-legacy", binned)

    def test_an_auto_binned_ledger_is_deleted_thirty_days_later(self):
        with at(T0):
            create("life-purge", "--as", "w")
            finish("life-purge")
        ledger_bin.tidy(now=T0)
        self.assertIn("life-purge", ledger_bin.entries())
        ledger_bin.tidy(now=T0 + 30 * DAY_MS)
        self.assertTrue(repository.exists("life-purge"))
        ledger_bin.tidy(now=T0 + 30 * DAY_MS + 1)
        self.assertFalse(repository.exists("life-purge"))


class Restore(unittest.TestCase):
    def test_a_restored_done_ledger_stays_on_home_until_idle_again(self):
        with at(T0):
            create("back-done", "--as", "w")
            finish("back-done")
        bin_storage.auto_bin(now=T0 + 1000)
        self.assertTrue(ledger_bin.restore("back-done", now=T0 + DAY_MS))
        self.assertNotIn("back-done", bin_storage.auto_bin(now=T0 + 2 * DAY_MS))
        self.assertIn("back-done", {s["slug"] for s in server.ledger_summaries()})
        self.assertNotIn("back-done", bin_storage.auto_bin(now=T0 + 8 * DAY_MS))
        self.assertIn("back-done", bin_storage.auto_bin(now=T0 + 8 * DAY_MS + 1))

    def test_a_restored_idle_ledger_gets_a_fresh_idle_limit_and_stays_usable(self):
        with at(T0):
            create("back-idle", "--as", "w")
        bin_storage.auto_bin(now=T0 + 8 * DAY_MS)
        self.assertIn("back-idle", ledger_bin.entries())
        self.assertTrue(ledger_bin.restore("back-idle", now=T0 + 9 * DAY_MS))
        self.assertNotIn("back-idle", bin_storage.auto_bin(now=T0 + 10 * DAY_MS))
        with at(T0 + 11 * DAY_MS):
            _, rejected = core.sync("back-idle", ops=[{"op": "add", "thread": "chat", "id": "c1", "text": "back"}])
        self.assertEqual(rejected, [])
        self.assertNotIn("back-idle", bin_storage.auto_bin(now=T0 + 18 * DAY_MS))
        self.assertIn("back-idle", bin_storage.auto_bin(now=T0 + 18 * DAY_MS + 1))

    def test_a_change_after_restore_lets_a_finished_ledger_bin_again(self):
        with at(T0):
            create("back-again", "--as", "w")
            finish("back-again")
        bin_storage.auto_bin(now=T0 + 1000)
        ledger_bin.restore("back-again", now=T0 + DAY_MS)
        with at(T0 + 2 * DAY_MS):
            core.sync("back-again", ops=[{"op": "add", "thread": "chat", "id": "c1", "text": "all good"}])
        self.assertIn("back-again", bin_storage.auto_bin(now=T0 + 2 * DAY_MS + 1000))


if __name__ == "__main__":
    unittest.main()
