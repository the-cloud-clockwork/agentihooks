import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_agent_ops  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "gate-lift-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def lift(**fields):
    return {"op": "gate_lift", "id": "gate_lift-1", "by": "engineer@1-1", "gate": "talk", **fields}


class GateLiftOp(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_a_lift_is_recorded_as_a_gate_lifted_event_by_the_agent(self):
        state, _ = core.sync(SLUG, ops=[lift()])
        event = state["_meta"]["events"][-1]
        self.assertEqual(
            (event["by"], event["kind"], event["target"], event["text"]),
            ("engineer@1-1", "gate lifted", "", "the operator lifted the talk gate for one hour"),
        )

    def test_a_lift_is_an_agent_op(self):
        self.assertIn("gate_lift", core.AGENT_OPS)

    def test_a_lift_needs_a_gate_name(self):
        for gate in (None, "", "Talk gate", "a/b", "x" * 41, 3):
            with self.assertRaises(ValueError):
                core.check_op(lift(gate=gate))
        core.check_op(lift(gate="watch-budget"))
        core.check_op(lift(gate="x" * 40))

    def test_the_lift_handler_records_one_event_and_reports_a_change(self):
        recorded = []

        class Ctx:
            def record(self, *args, **extra):
                recorded.append((args, extra))

        self.assertIs(ledger_agent_ops.HANDLERS["gate_lift"]({}, lift(), Ctx()), True)
        self.assertEqual(
            recorded,
            [
                (
                    ("engineer@1-1", "gate lifted", ""),
                    {"text": "the operator lifted the talk gate for one hour", "gate": "talk"},
                )
            ],
        )

    def test_the_agent_op_check_names_the_missing_gate(self):
        with self.assertRaisesRegex(ValueError, "^gate_lift needs the gate's name$"):
            ledger_agent_ops.check(lift(gate=""))
        ledger_agent_ops.check(lift())
