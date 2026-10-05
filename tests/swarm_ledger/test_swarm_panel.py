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

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

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
            patch.object(server.subprocess, "run", side_effect=[completed(0), completed(1, "", "gone")]),
        ):
            code, _ = self.put({"action": "start"})
        self.assertEqual(code, 502)

    def test_page_has_controls_wired_to_the_endpoint(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        for control in (
            "start",
            "pause",
            "stop",
            "stop_now",
            "max_eng",
            "max_ci",
            "set",
            "eng_down",
            "eng_up",
            "ci_down",
            "ci_up",
        ):
            self.assertIn(f'data-swarm="{control}"', page)
        self.assertIn('method: "PUT"', page)
        self.assertIn("/api/swarm/", page)

    def test_panel_sits_in_the_sidebar_directly_under_stats(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        column = page.split('<div class="layout"><div class="col">', 1)[1].split("<aside", 1)[0]
        side = page.split('<aside class="side">', 1)[1].split("</aside>", 1)[0]
        self.assertNotIn("swarm", column)
        sections = re.findall(r"<section[^>]*>", side)
        self.assertEqual(len(sections), 2)
        self.assertIn('id="stats"', side.split(sections[1], 1)[0])
        self.assertIn('id="swarm-box"', sections[1])
        self.assertIn("hidden", sections[1])

    def test_sidebar_scrolls_on_its_own_pinned_to_the_viewport(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        side = re.search(r"^\.side \{([^}]*)\}", page, re.M).group(1)
        self.assertIn("position: sticky", side)
        self.assertRegex(side, r"max-height: calc\(100vh - \d+px\)")
        self.assertIn("overflow-y: auto", side)
        self.assertRegex(page, r"\.side > section \{[^}]*flex: none")
        narrow = page.split("@media (max-width: 1100px) {", 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(narrow, r"\.side \{[^}]*position: static; max-height: none; overflow: visible")

    def test_agent_list_takes_its_natural_height_under_a_sticky_header(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        rule = re.search(r"\.sw-list \{([^}]*)\}", page).group(1)
        self.assertNotIn("max-height", rule)
        self.assertNotIn("overflow", rule)
        title = re.search(r"#swarm-fold > summary \{([^}]*)\}", page).group(1)
        self.assertIn("position: sticky; top: 0", title)
        head = re.search(r"\.sw-head \{([^}]*)\}", page).group(1)
        self.assertIn("position: sticky; top: var(--sw-sum)", head)
        for rule in (title, head):
            self.assertRegex(rule, r"background: var\(--panel\)")
        self.assertRegex((SCRIPTS / "palette.css").read_text(encoding="utf-8"), r"--panel: var\(--[a-z]+-\d{2,3}\);")
        box = page.split('id="swarm-box"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="swarm-state"', box.split("<summary>", 1)[1].split("</summary>", 1)[0])
        header = box.split('<div class="sw-head">', 1)[1].split('<ul class="sw-list"', 1)[0]
        for part in ('id="swarm-figs"', 'id="swarm-ctl"', 'id="cap-eng"', 'id="swarm-note"'):
            self.assertIn(part, header)

    def test_swarm_styles_use_only_palette_tokens(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        rules = re.findall(r"^(?:\.sw-|#swarm)[^{]*\{[^}]*\}", page, re.M)
        self.assertGreater(len(rules), 5)
        for rule in rules:
            self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule)

    def run_js(self, names, expr):
        script = "".join(function_source(n) + "\n" for n in names) + f"process.stdout.write(JSON.stringify({expr}));"
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_cards_show_task_titles_pull_requests_status_model_and_last_activity(self):
        sw = {
            "agents": [
                {
                    "name": "s-eng-7",
                    "lane": "eng",
                    "harness": "claude",
                    "account": "acct",
                    "task": "t7",
                    "model": "opus",
                    "effort": "high",
                    "started_at": 1000,
                    "status": "working",
                },
                {
                    "name": "s-ci-4",
                    "lane": "ci",
                    "harness": "codex",
                    "account": "",
                    "task": "ci-split-tests",
                    "started_at": 1000,
                    "status": "stalled",
                },
            ]
        }
        tasks = [
            {"id": "t7", "title": "Handoff at the compact limit", "pr_url": "https://x/pull/205"},
            {"id": "ci-split-tests", "title": "Split the suite across more runners", "pr_url": ""},
        ]
        now = 1000 + 3 * 3600_000 + 5 * 60_000
        seen = {"s-eng-7": now - 120_000}
        cards = self.run_js(
            ["span", "swarmCards"], f"swarmCards({json.dumps(sw)}, {json.dumps(tasks)}, {json.dumps(seen)}, {now})"
        )
        self.assertEqual(
            cards,
            [
                {
                    "name": "s-eng-7",
                    "lane": "eng",
                    "model": "opus high",
                    "account": "acct",
                    "task": "Handoff at the compact limit",
                    "pr": "https://x/pull/205",
                    "status": "working",
                    "ago": "2m",
                },
                {
                    "name": "s-ci-4",
                    "lane": "ci",
                    "model": "unknown",
                    "account": "",
                    "task": "Split the suite across more runners",
                    "pr": "",
                    "status": "stalled",
                    "ago": "3h 5m",
                },
            ],
        )

    def test_the_master_card_comes_first_and_says_it_answers_the_chat(self):
        sw = {
            "agents": [
                {"name": "s-eng-1", "lane": "eng", "task": "t1", "status": "working"},
                {"name": "s-master-2", "lane": "master", "task": "master", "status": "idle"},
            ]
        }
        cards = self.run_js(["span", "swarmCards"], f"swarmCards({json.dumps(sw)}, [], {{}}, 5)")
        self.assertEqual(
            [(c["name"], c["lane"], c["task"]) for c in cards][0], ("s-master-2", "master", "Answers your chat")
        )
        self.assertEqual(cards[1]["name"], "s-eng-1")

    def test_the_master_card_is_marked_for_its_own_style(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        self.assertIn('a.lane === "master" ? "sw-card master" : "sw-card"', page)
        self.assertRegex(page, r"\.sw-card\.master \.sw-name\s*\{")
        self.assertLess(page.index('id="swarm-master"'), page.index('<details class="sw-agents fold"'))

    def test_an_unknown_task_falls_back_to_its_id(self):
        sw = {"agents": [{"name": "a", "lane": "eng", "task": "gone", "status": "idle"}]}
        (card,) = self.run_js(["span", "swarmCards"], f"swarmCards({json.dumps(sw)}, [], {{}}, 5)")
        self.assertEqual((card["task"], card["status"], card["ago"]), ("gone", "idle", ""))

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

    def test_stop_now_asks_for_a_confirm_before_it_is_sent(self):
        out = self.run_js(
            ["controlClick"],
            '[controlClick("stop_now", ""), controlClick("stop_now", "stop_now"), controlClick("start", "stop_now"),'
            ' controlClick("pause", "")]',
        )
        self.assertEqual(
            out,
            [
                {"send": False, "armed": "stop_now"},
                {"send": True, "armed": ""},
                {"send": True, "armed": ""},
                {"send": True, "armed": ""},
            ],
        )

    def test_each_op_reports_pending_then_done_or_error(self):
        out = self.run_js(
            ["opNote"],
            '[opNote("pending", "stop_now"), opNote("done", "set"), opNote("error", "start", "no swarm x")]',
        )
        self.assertEqual(
            out,
            [
                {"cls": "pending", "text": "Stop now: sending"},
                {"cls": "ok", "text": "Set caps: done"},
                {"cls": "bad", "text": "Start failed: no swarm x"},
            ],
        )

    def test_a_cap_step_sets_one_lane_from_its_current_cap_within_0_and_50(self):
        config = {"max_eng": 2, "max_ci": 0}
        out = self.run_js(
            ["capStep"],
            f"[capStep({json.dumps(config)}, 'eng', 1), capStep({json.dumps(config)}, 'eng', -1),"
            f" capStep({json.dumps(config)}, 'ci', -1), capStep({{max_eng: 50, max_ci: 1}}, 'eng', 1)]",
        )
        self.assertEqual(
            out,
            [
                {"action": "set", "max_eng": 3},
                {"action": "set", "max_eng": 1},
                {"action": "set", "max_ci": 0},
                {"action": "set", "max_eng": 50},
            ],
        )

    def test_step_buttons_at_a_bound_are_disabled(self):
        out = self.run_js(["capBounds"], "[capBounds({max_eng: 0, max_ci: 50}), capBounds({max_eng: 3, max_ci: 1})]")
        self.assertEqual(
            out,
            [
                {"eng_down": True, "eng_up": False, "ci_down": False, "ci_up": True},
                {"eng_down": False, "eng_up": False, "ci_down": False, "ci_up": False},
            ],
        )

    def test_the_crew_box_shows_only_on_a_ledger_without_a_swarm(self):
        out = self.run_js(
            ["crewShown"],
            f'[crewShown([{{name: "a"}}], {json.dumps(STATUS)}), crewShown([{{name: "a"}}], null), crewShown([], null)]',
        )
        self.assertEqual(out, [False, True, False])

    def test_each_swarm_poll_hides_or_restores_the_crew_box(self):
        stubs = (
            "const els = {}; const $ = (id) => els[id] || (els[id] = {hidden: true, replaceChildren() {}});"
            "const h = () => ({}); const document = {}; const FIGURES = []; let doc = null; let swarm = null;"
            "const renderControls = () => {}; const swarmCards = () => []; const swarmCard = () => ({});"
            'const meta = {crew: [{name: "a"}]};'
        )
        script = (
            stubs
            + "".join(function_source(n) + "\n" for n in ("crewShown", "renderSwarm"))
            + f"renderSwarm({json.dumps(STATUS)}); const withSwarm = $('crew-box').hidden;"
            + "renderSwarm(null); process.stdout.write(JSON.stringify([withSwarm, $('crew-box').hidden]));"
        )
        out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out, [True, False])

    def test_step_ops_report_pending_then_done_or_error_by_name(self):
        out = self.run_js(
            ["opNote"],
            '[opNote("pending", "eng_up"), opNote("done", "ci_down"), opNote("error", "eng_down", "no swarm x")]',
        )
        self.assertEqual(
            out,
            [
                {"cls": "pending", "text": "Raise eng cap: sending"},
                {"cls": "ok", "text": "Lower ci cap: done"},
                {"cls": "bad", "text": "Lower eng cap failed: no swarm x"},
            ],
        )

    def test_a_one_lane_set_runs_only_that_lane(self):
        code, _, run = self.control({"action": "set", "max_ci": 2})
        self.assertEqual(code, 200)
        self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", "max-ci-agents=2"])

    def test_a_cap_label_targets_its_input_not_a_step_button(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        for lane in ("eng", "ci"):
            label = re.search(rf"<label[^>]*>{lane} <button[^>]*data-swarm=\"{lane}_down\"", page).group(0)
            self.assertIn(f'for="cap-{lane}"', label)


if __name__ == "__main__":
    unittest.main()
