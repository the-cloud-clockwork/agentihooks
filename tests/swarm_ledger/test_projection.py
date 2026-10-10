import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger  # noqa: E402
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger.repository import repository as storage  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "time_left_minutes-2026-01-01"


class TimeLeft(unittest.TestCase):
    def setUp(self):
        content = {
            "title": "Demo",
            "overview": "o",
            "sources": [],
            "phases": [{"title": "one", "description": "d"}],
            "questions": [],
            "followups": [],
        }
        html_path, json_path = core.paths(SLUG)
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
        json_path.unlink(missing_ok=True)
        storage.apply_ops(SLUG)

    def test_cli_persists_an_attributed_estimate_and_status_returns_it(self):
        args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "time-left", "3h 20m"])

        def call(slug, ops=None):
            if ops:
                core.check_body({"ops": ops})
            state, rejected = storage.apply_ops(slug, ops=ops)
            return {**state, "rejected": rejected}

        with patch.object(ledger, "call", side_effect=call), contextlib.redirect_stdout(io.StringIO()) as output:
            with patch.object(sys, "argv", ["ledger.py", "--slug", SLUG, "--as", "boss", "time-left", "3h 20m"]):
                ledger.main()
            output.seek(0)
            output.truncate()
            ledger.cmd_status(args)
            self.assertEqual(json.loads(output.getvalue())["time_left_minutes"], 200)
        state = storage.apply_ops(SLUG)[0]
        self.assertEqual(state["time_left_minutes"], 200)
        event = state["_meta"]["events"][-1]
        self.assertEqual(
            (event["by"], event["kind"], event["target"], event["text"]),
            ("boss", "time left changed", "time_left_minutes", "200m"),
        )
        self.assertGreater(event["at"], 0)

    def test_server_accepts_only_nonnegative_whole_minutes(self):
        for value in (0, 20, 200, 1500):
            core.check_body(
                {"ops": [{"op": "set", "id": "estimate", "by": "boss", "path": "time_left_minutes", "value": value}]}
            )
        for value in (False, True, -1, 20.5, None, [], "", "20%", "3h", "State at 12:23Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.check_body(
                    {
                        "ops": [
                            {"op": "set", "id": "estimate", "by": "boss", "path": "time_left_minutes", "value": value}
                        ]
                    }
                )
        with self.assertRaises(ValueError):
            core.check_body({"ops": [{"op": "set", "id": "estimate", "by": "boss", "path": "projection", "value": 85}]})

    def test_cli_parses_durations_and_rejects_prose_and_percentages(self):
        parser = ledger.build_parser()
        for value, expected in (("20", 20), ("0m", 0), ("3h", 180), ("20m", 20), ("3h 20m", 200), ("25h", 1500)):
            args = parser.parse_args(["--slug", SLUG, "--as", "boss", "time-left", value])
            self.assertEqual(args.minutes, expected)
        for value in ("State at 12:23Z", "-1", "85%", "20.5m", "20m done", "+20", "２０m", ""):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["--slug", SLUG, "--as", "boss", "time-left", value])

    def test_stats_show_one_duration_independent_of_time_and_completion(self):
        from tests.swarm_ledger.test_fold import function_source

        script = """
const nodes = {}; const $ = id => nodes[id] ||= {replaceChildren: (...rows) => {nodes[id].textContent = rows.join(" · ");}};
const h = (tag, attrs, ...children) => attrs.text || children.filter(Boolean).join(" ");
const when = () => "";
const inScope = list => list;
const activeAgents = () => 0;
const swarm = null;
const inboxPending = () => 0;
let meta = {created_at: Date.now() - 3600000};
let doc = {phases: [{done: true}, {done: false}], followups: [], questions: [], tasks: [], time_left_minutes: 200};
"""
        script += function_source("span") + function_source("timeLeftInputs") + function_source("renderStats")
        script += """
const assert = require("node:assert/strict");
const timeLeft = () => { renderStats(); return $("stats").textContent.split(" · ")[2]; };
assert.equal(timeLeft(), "Time left 3h 20m");
meta.created_at -= 86400000;
doc.phases[1].done = true;
assert.equal(timeLeft(), "Time left 3h 20m");
doc.time_left_minutes = 0;
assert.equal(timeLeft(), "Time left 0m");
doc.time_left_minutes = null;
assert.equal(timeLeft(), "Time left not set");
doc.time_left_minutes = 20;
assert.equal(timeLeft(), "Time left 20m");
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
