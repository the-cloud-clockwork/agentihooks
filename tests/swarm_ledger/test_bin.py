import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_bin  # noqa: E402
from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger.repository import bin_storage, repository
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import browser_home

DAY_MS = 24 * 60 * 60 * 1000


def make_ledger(slug, title="T", overview="O"):
    content = {"title": title, "overview": overview, "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(slug)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), slug, 8765), encoding="utf-8")
    core.sync(slug)
    return html_path, json_path


class BinState(unittest.TestCase):
    def slugs(self):
        return {s["slug"] for s in server.ledger_summaries()}

    def test_delete_hides_the_ledger_from_home_and_lists_it_in_the_bin(self):
        make_ledger("hide-me", "Hide me", "Hidden overview")
        ledger_bin.delete("hide-me", now=1000)
        self.assertNotIn("hide-me", self.slugs())
        binned = {s["slug"]: s for s in server.bin_summaries(now=1000 + 2 * DAY_MS)}
        self.assertEqual(binned["hide-me"]["title"], "Hide me")
        self.assertEqual(binned["hide-me"]["overview"], "Hidden overview")
        self.assertEqual(binned["hide-me"]["days_left"], 28)
        self.assertEqual(binned["hide-me"]["deleted_at"], 1000)
        self.assertTrue(repository.exists("hide-me"))

    def test_restore_returns_the_ledger_to_home(self):
        make_ledger("bring-back")
        ledger_bin.delete("bring-back", now=1000)
        ledger_bin.restore("bring-back")
        self.assertIn("bring-back", self.slugs())
        self.assertNotIn("bring-back", ledger_bin.entries())

    def test_item_past_thirty_days_is_purged_for_good(self):
        make_ledger("old-one")
        ledger_bin.delete("old-one", now=0)
        purged = bin_storage.purge_expired(now=30 * DAY_MS + 1)
        self.assertEqual(purged, ["old-one"])
        self.assertFalse(repository.exists("old-one"))
        self.assertNotIn("old-one", ledger_bin.entries())

    def test_item_under_thirty_days_is_kept(self):
        make_ledger("young-one")
        ledger_bin.delete("young-one", now=0)
        self.assertEqual(bin_storage.purge_expired(now=29 * DAY_MS), [])
        self.assertTrue(repository.exists("young-one"))
        self.assertIn("young-one", ledger_bin.entries())

    def test_home_has_a_delete_control_per_row_and_the_bin_view_a_restore_control(self):
        make_ledger("shown")
        make_ledger("binned")
        ledger_bin.delete("binned")
        home = browser_home(server)
        self.assertIn('data-act="delete" data-slug="shown"', home)
        self.assertNotIn('href="/binned"', home)
        self.assertIn('id="bin-fab"', home)
        view = browser_home(server, "bin")
        self.assertIn('data-act="restore" data-slug="binned"', view)
        self.assertIn("30 days left", view)
        self.assertNotIn('data-slug="shown"', view)


class BinEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        make_ledger("via-http")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, args=(0.01,), daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        bin_storage.restore("via-http")
        no_swarm = patch.object(server, "swarm_status", return_value=None)
        no_swarm.start()
        self.addCleanup(no_swarm.stop)

    def post(self, body, origin=None, host=None):
        headers = {"Host": host or f"127.0.0.1:{server.PORT}", "Content-Type": "application/json"}
        headers["Origin"] = origin or f"http://127.0.0.1:{server.PORT}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/bin", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_delete_then_restore_over_http(self):
        self.assertEqual(self.post({"action": "delete", "slug": "via-http"})[0], 200)
        self.assertIn("via-http", ledger_bin.entries())
        self.assertEqual(self.post({"action": "restore", "slug": "via-http"})[0], 200)
        self.assertNotIn("via-http", ledger_bin.entries())

    def test_delete_stops_the_ledgers_swarm_and_restore_does_not_start_it(self):
        with (
            patch.object(server, "swarm_status", return_value={"state": "running"}),
            patch.object(server, "swarm_control", return_value=({"state": "stopped"}, "")) as control,
        ):
            self.assertEqual(self.post({"action": "delete", "slug": "via-http"})[0], 200)
            control.assert_called_once_with("via-http", ["stop", "--now"])
            self.assertIn("via-http", ledger_bin.entries())
            self.assertEqual(self.post({"action": "restore", "slug": "via-http"})[0], 200)
            control.assert_called_once()

    def test_a_ledger_without_a_swarm_bins_as_before(self):
        with (
            patch.object(server, "swarm_status", return_value=None),
            patch.object(server, "swarm_control") as control,
        ):
            self.assertEqual(self.post({"action": "delete", "slug": "via-http"})[0], 200)
        control.assert_not_called()
        self.assertIn("via-http", ledger_bin.entries())

    def test_forged_origin_is_refused(self):
        code, _ = self.post({"action": "delete", "slug": "via-http"}, origin="http://evil.example")
        self.assertEqual(code, 403)
        self.assertNotIn("via-http", ledger_bin.entries())

    def test_foreign_host_is_refused(self):
        code, _ = self.post({"action": "delete", "slug": "via-http"}, host="evil.example")
        self.assertEqual(code, 403)
        self.assertNotIn("via-http", ledger_bin.entries())

    def test_unknown_ledger_and_bad_action_are_rejected(self):
        self.assertEqual(self.post({"action": "delete", "slug": "no-such"})[0], 404)
        self.assertEqual(self.post({"action": "restore", "slug": "via-http"})[0], 404)
        self.assertEqual(self.post({"action": "nuke", "slug": "via-http"})[0], 400)


def test_purge_removes_only_the_deleted_ledgers_operator_log(tmp_path, monkeypatch):
    from hooks.context import operator_words

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    make_ledger("old-one")
    make_ledger("other-one")
    operator_words.heard_prompt(
        "purged words", {"AGENTIHOOKS_AGENT_NAME": "master@a1-1", "AGENTIHOOKS_SWARM": "old-one"}, now=100
    )
    operator_words.heard_answer(
        {"tool_name": "AskUserQuestion", "tool_input": {"answers": {"q": "kept words"}}},
        {"AGENTIHOOKS_AGENT_NAME": "master@b2-1", "AGENTIHOOKS_SWARM": "other-one"},
        now=100,
    )
    ledger_bin.delete("old-one", now=0)
    assert operator_words.matching("master@a1-1", "purged words", within=None) == "purged words"
    assert bin_storage.purge_expired(now=30 * DAY_MS + 1) == ["old-one"]
    assert operator_words.matching("master@a1-1", "purged words", within=None) == ""
    assert operator_words.recorded("master@a1-*") == []
    assert operator_words.matching("master@b2-1", "kept words", within=None) == "kept words"
    assert operator_words.recorded("master@b2-*") == ["master@b2-1"]


if __name__ == "__main__":
    unittest.main()
