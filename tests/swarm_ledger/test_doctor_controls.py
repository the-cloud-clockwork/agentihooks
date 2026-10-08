import json
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.swarm_ledger.ledger_page import page_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.test_swarm_panel import STATUS, function_source  # noqa: E402

SLUG = "doctor-controls"


def run_js(names, expr):
    script = "".join(function_source(n) + "\n" for n in names) + f"process.stdout.write(JSON.stringify({expr}));"
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


class DoctorControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        content = {"title": "Demo", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
        new_ledger.create(SLUG, content)
        cls.token = legacy_page.stored_token(core.paths(SLUG)[0])
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, args=(0.01,), daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def put(self, path, body):
        headers = {"Host": f"127.0.0.1:{server.PORT}", "Content-Type": "application/json", "X-Ledger-Token": self.token}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", json.dumps(body).encode(), headers, method="PUT"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_doctor_controls_run_the_doctor_command_and_return_fresh_status(self):
        from scripts.swarm import commands
        from tests.swarm_ledger.test_swarm_panel import SwarmPanel

        for action, verb in (("doctor_start", "start"), ("doctor_stop", "stop")):
            saved = SwarmPanel().live_store()
            from scripts.swarm.store import SwarmConfig

            saved.create(SwarmConfig(SLUG, "/repo", 0, 0))
            with (
                patch.object(server, "swarm_store", return_value=saved),
                patch.object(commands, "submit", wraps=commands.submit) as queued,
                patch.object(server, "swarm_status", return_value=STATUS) as status,
            ):
                code, text = self.put(f"/api/swarm/{SLUG}", {"action": action})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(text), STATUS)
            self.assertEqual(queued.call_args.args[1:], (SLUG, "doctor", [verb]))
            status.assert_called_with(SLUG)

    def test_the_operator_phrase_in_chat_stops_the_doctor(self):
        with patch.object(server, "swarm_control", return_value=(STATUS, "")) as queued:
            self.put(f"/api/{SLUG}", {"ops": [{"op": "add", "id": "c1", "thread": "chat", "text": "Rig doctor stop"}]})
            self.put(f"/api/{SLUG}", {"ops": [{"op": "add", "id": "c2", "thread": "chat", "text": "how is it going"}]})
        queued.assert_called_once_with(SLUG, ["stop"], "doctor")

    def test_the_panel_carries_doctor_buttons(self):
        page = page_source()
        box = page.split('id="doctor-box"', 1)[1].split('id="health-box"', 1)[0]
        self.assertIn('data-swarm="doctor_start"', box)
        self.assertIn('data-swarm="doctor_stop"', box)

    def test_doctor_buttons_follow_the_doctor_slug(self):
        out = run_js(
            ["doctorOn"], '[doctorOn({doctor: {slug: ""}}), doctorOn({doctor: {slug: "watch-doctor"}}), doctorOn(null)]'
        )
        self.assertEqual(out, [False, True, False])

    def test_stop_doctor_asks_for_a_confirm_and_names_itself(self):
        page = page_source()
        self.assertIn('doctor_stop: "Stop the Doctor crew and close its linked ledger."', page)
        out = run_js(["opNote"], '[opNote("done", "doctor_start"), opNote("pending", "doctor_stop")]')
        self.assertEqual(
            out,
            [{"cls": "ok", "text": "Start doctor: acknowledged"}, {"cls": "pending", "text": "Stop doctor: sending"}],
        )
