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

    def test_operational_panels_render_inside_the_swarm_tab(self):
        page = (SCRIPTS / "template.html").read_text()
        swarm = page.split('id="swarm" role="tabpanel"', 1)[1].split("</main>", 1)[0]
        for marker in (
            "swarm-box",
            "swarm-ctl",
            "swarm-agents",
            "health-box",
            "capacity-box",
            "doctor-box",
            "crew-box",
        ):
            self.assertIn(f'id="{marker}"', swarm)
        self.assertNotIn('id="stats"', swarm)

    def test_the_tab_body_owns_page_scroll(self):
        page = (SCRIPTS / "template.html").read_text()
        self.assertRegex(page, r"\.tab-body \{[^}]*overflow-y: auto")
        self.assertIn("html, body { height: 100%; overflow: hidden; }", page)

    def test_agent_list_takes_its_natural_height_inside_the_tab(self):
        page = (SCRIPTS / "template.html").read_text()
        rule = re.search(r"\.sw-list \{([^}]*)\}", page).group(1)
        self.assertNotIn("max-height", rule)
        self.assertNotIn("overflow", rule)
        self.assertIn('id="swarm-tick"', page)
        self.assertIn('id="swarm-note"', page)

    def test_swarm_styles_use_only_palette_tokens(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        rules = re.findall(r"^(?:\.sw-|#swarm)[^{]*\{[^}]*\}", page, re.M)
        self.assertGreater(len(rules), 5)
        for rule in rules:
            self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule)

    def run_js(self, names, expr):
        script = "".join(function_source(n) + "\n" for n in names) + f"process.stdout.write(JSON.stringify({expr}));"
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_the_panel_header_shows_the_autonomy_level(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        box = page.split('id="swarm-box"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="swarm-autonomy"', box.split("<summary>", 1)[1].split("</summary>", 1)[0])
        self.assertIn('$("swarm-autonomy").textContent = autonomyText(c)', function_source("renderSwarm"))
        shown = self.run_js(["autonomyText"], '[autonomyText({}), autonomyText({ autonomy: "manual" })]')
        self.assertEqual(shown, ["autonomy delegate", "autonomy manual"])

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
                    "taskState": "open",
                    "held": "",
                    "taskId": "t7",
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
                    "taskState": "open",
                    "held": "",
                    "taskId": "ci-split-tests",
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
        self.assertLess(page.index('id="swarm-master"'), page.index('id="swarm-agents"'))

    def test_an_unknown_task_falls_back_to_its_id(self):
        sw = {"agents": [{"name": "a", "lane": "eng", "task": "gone", "status": "idle"}]}
        (card,) = self.run_js(["span", "swarmCards"], f"swarmCards({json.dumps(sw)}, [], {{}}, 5)")
        self.assertEqual((card["task"], card["status"], card["ago"]), ("gone", "idle", ""))

    def test_the_last_restore_lists_each_agent_resumed_or_fresh_with_its_reason_and_task_title(self):
        restored = [
            {
                "name": "s-eng-1",
                "lane": "eng",
                "task": "t1",
                "outcome": "resumed",
                "reason": "own conversation reopened",
            },
            {"name": "s-eng-2", "lane": "eng", "task": "gone", "outcome": "fresh", "reason": "worktree gone"},
        ]
        tasks = [{"id": "t1", "title": "Parse the config"}]
        rows = self.run_js(["restoreCards"], f"restoreCards({json.dumps(restored)}, {json.dumps(tasks)})")
        self.assertEqual(
            rows,
            [
                {
                    "name": "s-eng-1",
                    "outcome": "resumed",
                    "reason": "own conversation reopened",
                    "task": "Parse the config",
                },
                {"name": "s-eng-2", "outcome": "fresh", "reason": "worktree gone", "task": "gone"},
            ],
        )
        self.assertEqual(self.run_js(["restoreCards"], "restoreCards(undefined, [])"), [])

    def test_the_restore_box_folds_and_is_hidden_until_a_restore(self):
        page = (SCRIPTS / "template.html").read_text()
        self.assertIn('id="restore-box" hidden', page)
        self.assertIn('id="swarm-restore-box" hidden', page)
        self.assertIn('$("restore-box").hidden = !restored.length', function_source("renderSwarm"))
        self.assertIn("Restored after a reboot:", function_source("renderSwarm"))

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
                {"cls": "bad", "text": "Could not start the swarm: no swarm x. Try again or ask the master."},
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
                {
                    "eng_down": True,
                    "eng_up": False,
                    "ci_down": False,
                    "ci_up": True,
                    "codex_down": False,
                    "codex_up": False,
                },
                {
                    "eng_down": False,
                    "eng_up": False,
                    "ci_down": False,
                    "ci_up": False,
                    "codex_down": False,
                    "codex_up": False,
                },
            ],
        )

    def test_the_crew_box_shows_only_on_a_ledger_without_a_swarm(self):
        out = self.run_js(
            ["crewShown"],
            f'[crewShown([{{name: "a"}}], {json.dumps(STATUS)}), crewShown([{{name: "a"}}], null), crewShown([], null)]',
        )
        self.assertEqual(out, [False, True, False])

    def test_crew_history_remains_in_the_swarm_tab(self):
        page = (SCRIPTS / "template.html").read_text()
        self.assertIn('<section id="history-box">', page)
        self.assertIn('id="crew-box"', page)
        self.assertIn("renderCrew();", function_source("renderSwarm"))


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
                {"cls": "bad", "text": "Could not lower eng cap: no swarm x. Try again or ask the master."},
            ],
        )

    def test_a_one_lane_set_runs_only_that_lane(self):
        code, _, run = self.control({"action": "set", "max_ci": 2})
        self.assertEqual(code, 200)
        self.assertEqual(run.call_args_list[0].args[0][1:], ["swarm", SLUG, "set", "max-ci-agents=2"])

    def test_a_cap_label_targets_its_input_not_a_step_button(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        for lane in ("eng", "ci"):
            label = re.search(rf"<label[^>]*>[^<]+<button[^>]*data-swarm=\"{lane}_down\"", page).group(0)
            self.assertIn(f'for="cap-{lane}"', label)


if __name__ == "__main__":
    unittest.main()


FINDINGS = [
    {
        "kind": "idle with claim",
        "subject": "s-eng-1",
        "summary": "idle for 4 ticks while holding a task",
        "evidence": ["task Fold the chat panel (claimed)"],
        "threshold": "3 idle ticks",
    },
    {
        "kind": "scope inflation",
        "subject": "s-eng-2",
        "summary": "queued 3 tasks for its own lane",
        "evidence": ["Split the parser, gain 4", "Cache the index, gain 1.5", "q2, no gain stated"],
        "threshold": "3 self queued tasks whose gain never rose",
    },
]


PICK = "|false positive|early real|established|insufficient evidence|resolved|Give verdict"


class HealthPanel(unittest.TestCase):
    def render(self, findings):
        stubs = (
            "const els = {}; const $ = (id) => els[id] || (els[id] = {replaceChildren(...k) { this.kids = k; }});"
            "const h = (tag, attrs, ...kids) => ({tag, ...attrs, kids: kids.filter(Boolean)});"
        )
        script = (
            stubs
            + "".join(function_source(n) + "\n" for n in ("healthCard", "renderHealth"))
            + f"renderHealth({json.dumps(findings)});"
            + "const text = (n) => [n.text || '', ...(n.kids || []).map(text)].join('|').replace(/\\|+/g, '|');"
            + "process.stdout.write(JSON.stringify([els['health-count'].textContent, els['health'].kids.map(text)]));"
        )
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_each_finding_shows_its_subject_kind_summary_evidence_and_threshold(self):
        count, cards = self.render(FINDINGS)
        self.assertEqual(count, "· 2")
        self.assertEqual(
            cards,
            [
                "|s-eng-1|idle with claim|idle for 4 ticks while holding a task|task Fold the chat panel (claimed)"
                "|threshold 3 idle ticks" + PICK,
                "|s-eng-2|scope inflation|queued 3 tasks for its own lane|Split the parser, gain 4"
                "|Cache the index, gain 1.5|q2, no gain stated|threshold 3 self queued tasks whose gain never rose"
                + PICK,
            ],
        )

    def bullets(self, findings):
        stubs = "const h = (tag, attrs, ...kids) => ({tag, ...attrs, kids: kids.filter(Boolean)});"
        script = (
            stubs
            + function_source("healthCard")
            + f"\nconst cards = {json.dumps(findings)}.map(healthCard);"
            + "const order = (c) => c.kids.map((k) => k.class);"
            + "const list = (c) => c.kids.find((k) => k.class === 'hl-evidence');"
            + "process.stdout.write(JSON.stringify(cards.map((c) => "
            + "[order(c), list(c).tag, list(c).kids.map((k) => [k.tag, k.text])])));"
        )
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_a_scope_inflation_finding_with_three_tasks_renders_three_bullets(self):
        [(order, tag, items)] = self.bullets([FINDINGS[1]])
        self.assertEqual(order, ["sw-top", "hl-summary", "hl-evidence", "hl-threshold", "hl-verdict"])
        self.assertEqual(tag, "ul")
        self.assertEqual(
            items,
            [["li", "Split the parser, gain 4"], ["li", "Cache the index, gain 1.5"], ["li", "q2, no gain stated"]],
        )

    def test_a_finding_with_one_entry_renders_one_bullet(self):
        [(_, tag, items)] = self.bullets([FINDINGS[0]])
        self.assertEqual((tag, items), ("ul", [["li", "task Fold the chat panel (claimed)"]]))

    def test_each_card_offers_the_five_verdicts_for_its_finding(self):
        stubs = "const h = (tag, attrs, ...kids) => ({tag, ...attrs, kids: kids.filter(Boolean)});"
        script = (
            stubs
            + function_source("healthCard")
            + f"\nconst card = healthCard({json.dumps({**FINDINGS[0], 'id': 'idle-with-claim/s-eng-1'})});"
            + "const pick = card.kids.find((k) => k.class === 'hl-verdict');"
            + "process.stdout.write(JSON.stringify(pick.kids.map((k) => [k.tag, k['data-verdict'] || '',"
            + " (k.kids || []).map((o) => o.value)])));"
        )
        out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(
            out,
            [
                ["select", "", ["false-positive", "early-real", "established", "insufficient-evidence", "resolved"]],
                ["input", "", []],
                ["button", "idle-with-claim/s-eng-1", []],
            ],
        )

    def test_a_finding_back_after_its_cooldown_shows_its_earlier_verdict(self):
        back = {**FINDINGS[0], "verdict": {"value": "early-real", "note": "watch it", "by": "operator", "at": 1}}
        _, [card] = self.render([back])
        self.assertIn("|earlier verdict early real by operator: watch it|", card)

    def test_a_verdict_click_sends_the_picked_verdict_and_note(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        self.assertIn('$("swarm-box").addEventListener("click"', page)
        self.assertRegex(page, r'action: "verdict", id: btn\.dataset\.verdict')

    def test_no_findings_says_so(self):
        self.assertEqual(self.render([]), ["", ["No health findings. The swarm checks every minute."]])

    def test_the_swarm_health_renders_findings_with_the_swarm(self):
        source = function_source("renderSwarm")
        self.assertIn("renderHealth(sw.findings)", source)
        self.assertIn("renderNeedsYou(sw)", source)


    def test_health_styles_use_only_palette_tokens(self):
        page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        rules = re.findall(r"^\.hl-[^{]*\{[^}]*\}", page, re.M)
        self.assertGreaterEqual(len(rules), 3)
        for rule in rules:
            self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule)
