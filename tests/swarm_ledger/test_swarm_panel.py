import json
import re
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG = "swarm-panel-2026-01-01"
STATUS = {
    "config": {"slug": "s", "max_eng": 2, "max_ci": 1, "state": "running"},
    "agents": [
        {
            "name": "s-eng-1",
            "lane": "eng",
            "harness": "codex",
            "account": "a",
            "task": "t1",
            "state": "working",
            "model": "gpt-6.1-sol",
            "effort": "high",
        }
    ],
    "tasks": {"open": 1, "claimed": 1, "blocked": 0, "pr": 0, "done": 2},
}


def function_source(name):
    page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
    return f"function {name}(" + page.split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


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
        threading.Thread(target=cls.httpd.serve_forever, args=(0.01,), daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, slug=SLUG, token=True, timeout=None):
        headers = {"Host": f"127.0.0.1:{server.PORT}"}
        if token:
            headers["X-Ledger-Token"] = self.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/swarm/{slug}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def live_store(self):
        import fakeredis

        from scripts.swarm.store import RedisStore, SwarmConfig

        store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        store.create(SwarmConfig(SLUG, "/repo", 2, 1))
        return store

    def test_endpoint_builds_the_status_in_process_while_the_sync_lock_is_held(self):
        task = {
            "op": "task_add",
            "id": "task_add-panel",
            "by": "t",
            "task": "p1",
            "title": "One",
            "description": "d",
            "lane": "eng",
        }
        self.assertEqual(core.sync(SLUG, ops=[task])[1], [])
        refuse = AssertionError("the status read must not call back into the ledger server")
        with (
            patch.object(server, "swarm_store", return_value=self.live_store()),
            patch.object(server.subprocess, "run", side_effect=refuse) as run,
            patch("scripts.swarm.ledger_client.LedgerClient._call", side_effect=refuse),
            core.LOCK,
        ):
            code, body = self.get(timeout=10)
        self.assertEqual(code, 200, body)
        status = json.loads(body)
        self.assertEqual(status["config"]["slug"], SLUG)
        self.assertEqual(status["agents"], [])
        self.assertEqual(status["tasks"], {"open": 1, "claimed": 0, "blocked": 0, "pr": 0, "done": 0})
        run.assert_not_called()

    def test_endpoint_is_404_when_the_slug_has_no_swarm(self):
        import fakeredis

        from scripts.swarm.store import RedisStore

        empty = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        with patch.object(server, "swarm_store", return_value=empty):
            code, _ = self.get()
        self.assertEqual(code, 404)

    def test_endpoint_needs_the_ledger_token(self):
        with patch.object(server.subprocess, "run") as run:
            code, _ = self.get(token=False)
        self.assertEqual(code, 403)
        run.assert_not_called()

    def put(self, body, token=True):
        headers = {"Host": f"127.0.0.1:{server.PORT}", "Content-Type": "application/json"}
        if token:
            headers["X-Ledger-Token"] = self.token
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/swarm/{SLUG}", json.dumps(body).encode(), headers, method="PUT"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def control(self, body, result=None):
        result = result or completed(0, json.dumps(STATUS))
        with (
            patch.object(server.shutil, "which", return_value="agentihooks"),
            patch.object(server.subprocess, "run", return_value=result) as run,
            patch.object(server, "swarm_status", return_value=STATUS),
        ):
            code, text = self.put(body)
        return code, text, run

    def test_controls_run_the_matching_swarm_command(self):
        cases = {
            "start": ["start"],
            "pause": ["pause"],
            "stop": ["stop"],
            "stop_now": ["stop", "--now"],
        }
        for action, argv in cases.items():
            code, _, run = self.control({"action": action})
            self.assertEqual(code, 200)
            self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, *argv])

    def test_set_passes_both_caps_and_returns_fresh_status(self):
        code, text, run = self.control({"action": "set", "max_eng": 3, "max_ci": 0})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(text), STATUS)
        self.assertEqual(
            run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", "max-eng-agents=3", "max-ci-agents=0"]
        )

    def test_set_passes_codex_share_with_caps_and_returns_fresh_status(self):
        code, text, run = self.control({"action": "set", "max_eng": 3, "max_ci": 1, "codex_share": 45})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(text), STATUS)
        self.assertEqual(
            run.call_args_list[0].args[0][1:],
            ["swarm", SLUG, "set", "max-eng-agents=3", "max-ci-agents=1", "codex-share=45"],
        )
        for share in (0, 100):
            code, _, run = self.control({"action": "set", "codex_share": share})
            self.assertEqual(code, 200)
            self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", f"codex-share={share}"])

    def test_invalid_codex_share_never_runs_the_cli_or_changes_caps(self):
        for share in (-1, 101, 2.5, True, "30"):
            code, _, run = self.control({"action": "set", "max_eng": 3, "codex_share": share})
            self.assertEqual(code, 400, share)
            run.assert_not_called()

    def test_set_writes_the_swarm_compact_limit(self):
        for limit in (100, 450, 1000):
            code, _, run = self.control({"action": "set", "compact_limit": limit})
            self.assertEqual(code, 200)
            self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", f"compact-limit={limit}"])

    def test_a_compact_limit_outside_100_to_1000_never_runs_the_cli(self):
        for limit in (0, 99, 1001, 450.5, True, "450"):
            code, _, run = self.control({"action": "set", "compact_limit": limit})
            self.assertEqual(code, 400, limit)
            run.assert_not_called()

    def test_set_writes_each_autonomy_mode_and_refuses_others(self):
        for mode in ("manual", "assist", "delegate", "full"):
            code, _, run = self.control({"action": "set", "autonomy": mode})
            self.assertEqual(code, 200)
            self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", f"autonomy={mode}"])
        for mode in ("auto", "", 1, "full; rm"):
            code, _, run = self.control({"action": "set", "autonomy": mode})
            self.assertEqual(code, 400, mode)
            run.assert_not_called()

    def test_bad_requests_never_run_the_cli(self):
        for body in (
            {"action": "kill"},
            {"action": "set"},
            {"action": "set", "max_eng": -1},
            {"action": "set", "max_eng": "x"},
            {"action": "set", "max_ci": True},
            {"action": "set", "max_eng": 999},
        ):
            code, _, run = self.control(body)
            self.assertEqual(code, 400, body)
            run.assert_not_called()

    def test_a_verdict_runs_the_verdict_command_as_the_operator(self):
        code, _, run = self.control(
            {"action": "verdict", "id": "over-monitoring/s-master-1", "verdict": "false-positive", "note": "-re-arms"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(
            run.call_args_list[0].args[0][1:],
            [
                "swarm",
                SLUG,
                "--as",
                "operator",
                "verdict",
                "over-monitoring/s-master-1",
                "false-positive",
                "--note=-re-arms",
            ],
        )

    def test_a_bad_verdict_never_runs_the_cli(self):
        for body in (
            {"action": "verdict", "id": "over-monitoring/s-master-1", "verdict": "maybe"},
            {"action": "verdict", "id": "--as x", "verdict": "resolved"},
            {"action": "verdict", "verdict": "resolved"},
            {"action": "verdict", "id": "stale-claim/g1", "verdict": "resolved", "note": 5},
            {"action": "verdict", "id": "stale-claim/g1", "verdict": "resolved", "note": "x" * 501},
        ):
            code, _, run = self.control(body)
            self.assertEqual(code, 400, body)
            run.assert_not_called()

    def test_terminate_checks_membership_and_dry_run_before_signalling(self):
        live = {**STATUS, "agents": [{"name": "engineer-one"}]}
        with (
            patch.object(server, "swarm_status", return_value=live),
            patch.object(server.shutil, "which", return_value="agentihooks"),
            patch.object(server.subprocess, "run", return_value=completed(0)) as run,
        ):
            code, _ = self.put({"action": "terminate", "name": "engineer-one"})
        self.assertEqual(code, 200)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["agentihooks", "terminate-agent", "engineer-one", "--type", "any", "--dry-run"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0], ["agentihooks", "terminate-agent", "engineer-one", "--type", "any"]
        )
        with (
            patch.object(server, "swarm_status", return_value=live),
            patch.object(server.subprocess, "run") as run,
        ):
            code, _ = self.put({"action": "terminate", "name": "other-swarm-agent"})
        self.assertEqual(code, 502)
        run.assert_not_called()

    def test_quota_refresh_probes_every_account_and_answers_fresh_status(self):
        from scripts import agents_quota

        agents_quota._last_refresh.clear()
        agents_quota._page_cache.update(at=0.0, quota={"rows": []})
        code, text, run = self.control({"action": "quota_refresh"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(text), STATUS)
        self.assertEqual(run.call_args_list[0].args[0][1:], ["quota", "--refresh", "--json"])
        self.assertEqual(agents_quota._page_cache, {})
        code, _, again = self.control({"action": "quota_refresh"})
        self.assertEqual(code, 200)
        again.assert_not_called()

    def test_a_failed_quota_probe_is_502_with_its_message(self):
        from scripts import agents_quota

        agents_quota._last_refresh.clear()
        code, text, _ = self.control({"action": "quota_refresh"}, completed(1, err="no Claude account"))
        self.assertEqual((code, text), (502, "no Claude account"))

    def test_control_needs_the_ledger_token(self):
        with patch.object(server.subprocess, "run") as run:
            code, _ = self.put({"action": "start"}, token=False)
        self.assertEqual(code, 403)
        run.assert_not_called()

    def test_cli_failure_is_502_with_its_message(self):
        code, text, _ = self.control({"action": "start"}, completed(1, "", "swarm: no swarm x"))
        self.assertEqual(code, 502)
        self.assertIn("no swarm x", text)

    def test_unreadable_status_after_a_control_is_502(self):
        with (
            patch.object(server.shutil, "which", return_value="agentihooks"),
            patch.object(server.subprocess, "run", return_value=completed(0)),
            patch.object(server, "swarm_status", return_value=None),
        ):
            code, _ = self.put({"action": "start"})
        self.assertEqual(code, 502)

    def test_page_has_controls_wired_to_the_endpoint(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        for control in (
            "start",
            "pause",
            "stop",
            "doctor_start",
            "doctor_stop",
            "eng_down",
            "eng_up",
            "ci_down",
            "ci_up",
            "plan_down",
            "plan_up",
            "codex_down",
            "codex_up",
            "compact_down",
            "compact_up",
        ):
            self.assertIn(f'data-swarm="{control}"', page)
        for mode in ("manual", "assist", "delegate", "full"):
            self.assertIn(f'data-autonomy="{mode}"', page)
        self.assertIn('method: "PUT"', page)
        self.assertIn("/api/swarm/", page)

    def test_operational_blocks_render_inside_the_swarm_tab(self):
        page = (SCRIPTS / "template.html").read_text()
        swarm = page.split('id="swarm" role="tabpanel"', 1)[1].split('<aside id="stats-column"', 1)[0]
        for marker in (
            "swarm-box",
            "swarm-ctl",
            "swarm-modes",
            "swarm-agents",
            "swarm-tasks",
            "swarm-quota",
            "swarm-doctor",
            "health",
            "swarm-handoffs",
            "capacity-box",
            "swarm-tick",
            "swarm-note",
        ):
            self.assertIn(f'id="{marker}"', swarm)
        self.assertNotIn('id="stats"', swarm)

    def test_the_tab_body_owns_page_scroll(self):
        page = (SCRIPTS / "template.html").read_text()
        self.assertRegex(page, r"\.tab-body \{[^}]*overflow-y: auto")
        self.assertIn("html, body { height: 100%; overflow: hidden; }", page)

    def run_js(self, names, expr):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        consts = "".join(m + "\n" for m in re.findall(r"^  const (?:STEPS|LIVE_LANES) = .*;$", page, re.M))
        script = (
            consts
            + "".join(function_source(n) + "\n" for n in names)
            + f"process.stdout.write(JSON.stringify({expr}));"
        )
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_controls_that_do_not_apply_are_disabled(self):
        states = ["running", "paused", "stopping", "stopped", "drained"]
        out = self.run_js(["swarmControls"], f"{json.dumps(states)}.map(swarmControls)")
        self.assertEqual(
            dict(zip(states, out)),
            {
                "running": {"start": True, "pause": False, "stop": False, "stop_now": False},
                "paused": {"start": False, "pause": True, "stop": False, "stop_now": False},
                "stopping": {"start": False, "pause": True, "stop": True, "stop_now": False},
                "stopped": {"start": False, "pause": True, "stop": True, "stop_now": True},
                "drained": {"start": False, "pause": True, "stop": False, "stop_now": False},
            },
        )

    def test_each_op_reports_pending_then_done_or_error(self):
        out = self.run_js(
            ["opNote"],
            '[opNote("pending", "autonomy"), opNote("done", "compact_up"), opNote("error", "start", "no swarm x")]',
        )
        self.assertEqual(
            out,
            [
                {"cls": "pending", "text": "Set autonomy: sending"},
                {"cls": "ok", "text": "Raise compact limit: done"},
                {"cls": "bad", "text": "Could not start the swarm: no swarm x. Try again or ask the master."},
            ],
        )

    def test_a_cap_step_moves_one_setting_by_its_step_within_its_bounds(self):
        values = {"max_eng": 2, "max_ci": 0, "max_plan": 50, "codex_share": 98, "compact_limit": 600}
        steps = [
            ("eng", True),
            ("eng", False),
            ("ci", False),
            ("plan", True),
            ("codex", True),
            ("compact", True),
            ("compact", False),
        ]
        out = self.run_js(
            ["capStep"],
            "[" + ",".join(f"capStep({json.dumps(values)}, '{lane}', {str(up).lower()})" for lane, up in steps) + "]",
        )
        self.assertEqual(
            out,
            [
                {"action": "set", "max_eng": 3},
                {"action": "set", "max_eng": 1},
                {"action": "set", "max_ci": 0},
                {"action": "set", "max_plan": 50},
                {"action": "set", "codex_share": 100},
                {"action": "set", "compact_limit": 650},
                {"action": "set", "compact_limit": 550},
            ],
        )

    def test_step_buttons_at_a_bound_are_disabled(self):
        low = {"max_eng": 0, "max_ci": 50, "max_plan": 1, "codex_share": 100, "compact_limit": 100}
        out = self.run_js(["capBounds"], f"capBounds({json.dumps(low)})")
        self.assertEqual(
            out,
            {
                "eng_down": True,
                "eng_up": False,
                "ci_down": False,
                "ci_up": True,
                "plan_down": False,
                "plan_up": False,
                "codex_down": False,
                "codex_up": True,
                "compact_down": True,
                "compact_up": False,
            },
        )

    def test_a_one_lane_set_runs_only_that_lane(self):
        code, _, run = self.control({"action": "set", "max_ci": 2})
        self.assertEqual(code, 200)
        self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", "max-ci-agents=2"])

    def test_live_caps_count_working_agents_per_lane_against_their_cap(self):
        sw = {
            "config": {"max_eng": 3, "max_ci": 1, "max_plan": 1},
            "agents": [{"lane": "eng"}, {"lane": "eng", "state": "finished"}, {"lane": "ci"}, {"lane": "master"}],
        }
        self.assertEqual(self.run_js(["liveCaps"], f"liveCaps({json.dumps(sw)})"), ["eng 1/3", "ci 1/1", "plan 0/1"])

    def test_seat_names_read_as_lane_and_number_and_the_master_seat_stays_whole(self):
        out = self.run_js(["seatText"], '["master@rig", "eng-2@rig", "ci-1@rig"].map(seatText)')
        self.assertEqual(out, ["master@rig", "eng 2", "ci 1"])


if __name__ == "__main__":
    unittest.main()


class HealthPanel(unittest.TestCase):
    def test_a_verdict_pick_sends_the_picked_verdict_and_the_row_note(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        self.assertIn('$("health").addEventListener("change"', page)
        self.assertIn('swarmControl({ action: "verdict", id: pick.dataset.verdict, verdict: pick.value, note:', page)

    def test_the_swarm_health_renders_findings_with_the_swarm(self):
        source = function_source("renderSwarm")
        self.assertIn("renderHealth(sw.findings, now)", source)
        self.assertNotIn("renderNeedsYou", source)
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        self.assertNotIn("needs-you", page)
        self.assertNotIn("Needs you", page)

    def test_health_styles_use_only_palette_tokens(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        rules = re.findall(r"^\.hl-[^{]*\{[^}]*\}", page, re.M)
        self.assertGreaterEqual(len(rules), 3)
        for rule in rules:
            self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule)
