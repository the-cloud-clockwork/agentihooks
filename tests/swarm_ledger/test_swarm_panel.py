import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
os.environ["LEDGER_DIR"] = tempfile.mkdtemp(prefix="ledger-swarm-panel-test-")
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "swarm-panel-2026-01-01"
STATUS = {
    "config": {"slug": "s", "max_eng": 2, "max_ci": 1, "state": "running"},
    "agents": [
        {"name": "s-eng-1", "lane": "eng", "harness": "claude", "account": "a", "task": "t1", "state": "working"}
    ],
    "tasks": {"open": 1, "claimed": 1, "blocked": 0, "pr": 0, "done": 2},
}


def completed(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class SwarmPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        content = {"title": "Demo", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
        html_path, json_path = core.paths(SLUG)
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
        json_path.unlink(missing_ok=True)
        core.sync(SLUG)
        cls.token = core.read_token(html_path.read_text(encoding="utf-8"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, slug=SLUG, token=True):
        headers = {"Host": f"127.0.0.1:{server.PORT}"}
        if token:
            headers["X-Ledger-Token"] = self.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/swarm/{slug}", headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_endpoint_returns_the_status_json_from_the_swarm_cli(self):
        with (
            patch.object(server.shutil, "which", return_value="agentihooks"),
            patch.object(server.subprocess, "run", return_value=completed(0, json.dumps(STATUS))) as run,
        ):
            code, body = self.get()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), STATUS)
        self.assertEqual(run.call_args.args[0][1:], ["swarm", SLUG, "status", "--json"])

    def test_endpoint_is_404_when_the_slug_has_no_swarm(self):
        with (
            patch.object(server.shutil, "which", return_value="agentihooks"),
            patch.object(server.subprocess, "run", return_value=completed(1, "", "swarm: no swarm x")),
        ):
            code, _ = self.get()
        self.assertEqual(code, 404)

    def test_endpoint_needs_the_ledger_token(self):
        with patch.object(server.subprocess, "run") as run:
            code, _ = self.get(token=False)
        self.assertEqual(code, 403)
        run.assert_not_called()

    def test_page_has_a_hidden_swarm_panel_filled_from_the_endpoint(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        self.assertIn('id="swarm-box" hidden', page)
        self.assertIn("/api/swarm/", page)
        for field in ("max_eng", "max_ci", "lane", "harness", "account", "task", "state"):
            self.assertIn(field, page)


if __name__ == "__main__":
    unittest.main()
