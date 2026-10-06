import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_tasks  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "taskadd-2026-01-01"
PLANNER = "planner@abcdef-0003"
FOLLOWUP = 'propose the work with agentihooks ledger followup add "<plain words>" and the master decides'


def make_ledger(claims=()):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}, {"title": "two", "description": "d"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    for n, (lane, state, by) in enumerate(claims):
        kind = {"kind": "plan"} if lane == "plan" else {}
        fields = {"state": state, "claimed_by": by}
        core.sync(
            SLUG,
            ops=[
                {
                    "op": "task_add",
                    "id": f"seed-{n}",
                    "by": "swarm",
                    "task": f"c{n}",
                    "title": "c",
                    "phase": "p1",
                    "lane": lane,
                    **kind,
                },
                {"op": "task_update", "id": f"claim-{n}", "by": "swarm", "item": f"tasks/c{n}", "fields": fields},
            ],
        )


def add(by, phase="p1", task="t9"):
    return core.sync(
        SLUG,
        ops=[
            {"op": "task_add", "id": f"add-{task}", "by": by, "task": task, "title": "x", "phase": phase, "lane": "eng"}
        ],
    )


def plan_task(state="claimed", by=PLANNER):
    return ("plan", state, by)


class TaskAddGate(unittest.TestCase):
    def assert_refused(self, by, reason, phase="p1"):
        state, rejected = add(by, phase)
        self.assertEqual(rejected, ["add-t9"])
        self.assertEqual(state["_meta"]["warnings"], [reason])
        self.assertNotIn("t9", [t["id"] for t in state["tasks"]])

    def assert_added(self, by, phase="p1"):
        state, rejected = add(by, phase)
        self.assertEqual(rejected, [])
        self.assertIn("t9", [t["id"] for t in state["tasks"]])

    def test_an_engineer_cannot_add_a_task(self):
        make_ledger()
        self.assert_refused(
            "engineer@abcdef-0001", f"engineer@abcdef-0001 works in the eng lane and cannot add tasks: {FOLLOWUP}"
        )

    def test_a_ci_agent_cannot_add_a_task(self):
        make_ledger()
        self.assert_refused("ci@abcdef-0002", f"ci@abcdef-0002 works in the ci lane and cannot add tasks: {FOLLOWUP}")

    def test_a_legacy_engineer_name_cannot_add_a_task(self):
        make_ledger()
        self.assert_refused("sw-eng-1", f"sw-eng-1 works in the eng lane and cannot add tasks: {FOLLOWUP}")

    def test_master_operator_tick_and_doctor_add_tasks(self):
        for by in ("master@abcdef-0001", "operator", "swarm", "doctor", "sw-master-1"):
            with self.subTest(by=by):
                make_ledger()
                self.assert_added(by)

    def test_a_planner_adds_tasks_in_the_phase_it_plans(self):
        make_ledger([plan_task()])
        self.assert_added(PLANNER)

    def test_a_planner_cannot_add_a_task_outside_the_phase_it_plans(self):
        make_ledger([plan_task()])
        self.assert_refused(PLANNER, f"{PLANNER} plans phase p1 and cannot add a task to phase p2: {FOLLOWUP}", "p2")

    def test_a_planner_holding_no_plan_task_cannot_add_a_task(self):
        for tasks in ([], [plan_task("blocked")], [plan_task(by="planner@abcdef-0004")]):
            with self.subTest(tasks=tasks):
                make_ledger(tasks)
                self.assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}")

    def test_a_planner_engineer_task_in_its_phase_does_not_count_as_planning(self):
        make_ledger([("eng", "claimed", PLANNER)])
        self.assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}")

    def test_the_refusal_is_empty_for_an_author_who_may_add(self):
        self.assertEqual(ledger_tasks.add_refusal([], {"by": "master@abcdef-0001", "phase": "p1"}), "")


class TaskAddCli(unittest.TestCase):
    def test_task_add_prints_the_server_refusal(self):
        import ledger

        args = unittest.mock.Mock(slug=SLUG, name="engineer@abcdef-0001")
        reply = {"rejected": ["task_add-1"], "_meta": {"warnings": ["refused because"]}}
        with unittest.mock.patch.object(ledger, "call", return_value=reply):
            with self.assertRaises(SystemExit) as raised:
                ledger.send(args, "task_add", task="t9")
        self.assertEqual(raised.exception.code, "refused because")


if __name__ == "__main__":
    unittest.main()
