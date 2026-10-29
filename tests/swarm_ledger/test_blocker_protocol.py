import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_blockers as blockers  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "blocker-2026-01-01"
BOSS = "boss"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "followups": [{"text": "check disk"}],
    }
    html_path, json_path = legacy_page.paths(SLUG, core)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": BOSS, "role": "orchestrator"}])
    return state


def attempt(n, item, tried="restarted the runner"):
    return {
        "op": "blocker_attempt",
        "id": f"at-{n}",
        "by": BOSS,
        "item": item,
        "text": tried,
        "command": "kubectl rollout restart deployment/runner",
        "output": "deployment.apps/runner restarted",
    }


def critic(n, item, state, tier):
    row = blockers.find(state, item)
    prompt = blockers.blind_prompt(item, blockers.subject(row), tier, blockers.record(row)["attempts"])
    return {
        "op": "blocker_critic",
        "id": f"cr-{n}",
        "by": BOSS,
        "item": item,
        "run": "claude read only critic",
        "digest": blockers.digest(prompt),
        "resolution": "Scale the runner pool before restarting it.",
    }


def escalate(n, item):
    return {
        "op": "blocker_escalate",
        "id": f"es-{n}",
        "by": BOSS,
        "item": item,
        "reason": "The runner pool quota is spent until the weekly reset.",
        "evidence": "gh api rate_limit shows zero remaining",
    }


def priority(n, item):
    return {"op": "priority", "id": f"pr-{n}", "by": BOSS, "item": item, "text": "Decide the runner pool size."}


class Protocol(unittest.TestCase):
    tier = "full"

    def setUp(self):
        state = make_ledger()
        self.item = f"phases/{state['phases'][0]['id']}"
        patcher = mock.patch.object(blockers, "autonomy", side_effect=lambda slug: self.tier)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gate = blockers.Gate(SLUG)

    def write(self, *ops):
        state, rejected = core.sync(SLUG, ops=list(ops), gate=self.gate)
        return state, rejected

    def manual_priorities(self, state):
        return [p for p in state["priorities"] if not p.get("derived")]

    def full_record(self):
        self.write(attempt(1, self.item), attempt(2, self.item, "drained the node"))
        state, _ = self.write()
        state, rejected = self.write(critic(1, self.item, state, self.tier))
        self.assertEqual(rejected, [])
        state, rejected = self.write(attempt(3, self.item, "scaled the pool to four"))
        self.assertEqual(rejected, [])
        return state

    def test_at_full_a_priority_with_no_record_is_refused(self):
        state, rejected = self.write(priority(1, self.item))
        self.assertEqual(rejected, ["pr-1"])
        self.assertEqual(self.manual_priorities(state), [])
        self.assertIn("resolve attempt", " ".join(state["_meta"]["warnings"]))

    def test_at_full_an_escalation_with_no_record_is_refused(self):
        state, rejected = self.write(escalate(1, self.item))
        self.assertEqual(rejected, ["es-1"])
        self.assertEqual(self.manual_priorities(state), [])

    def test_at_full_two_attempts_and_no_critic_are_refused(self):
        self.write(attempt(1, self.item), attempt(2, self.item))
        state, rejected = self.write(escalate(1, self.item), priority(2, self.item))
        self.assertEqual(rejected, ["es-1", "pr-2"])
        self.assertEqual(self.manual_priorities(state), [])
        self.assertIn("resolve critic", " ".join(state["_meta"]["warnings"]))

    def test_a_third_attempt_before_the_critic_is_refused(self):
        _, rejected = self.write(attempt(1, self.item), attempt(2, self.item), attempt(3, self.item))
        self.assertEqual(rejected, ["at-3"])

    def test_a_critic_before_two_attempts_or_with_a_foreign_prompt_is_refused(self):
        self.write(attempt(1, self.item))
        state, _ = self.write()
        _, rejected = self.write(critic(1, self.item, state, self.tier))
        self.assertEqual(rejected, ["cr-1"])
        self.write(attempt(2, self.item))
        state, _ = self.write()
        forged = {**critic(2, self.item, state, self.tier), "digest": "0" * 64}
        _, rejected = self.write(forged)
        self.assertEqual(rejected, ["cr-2"])

    def test_the_critic_without_an_attempt_after_it_is_refused(self):
        self.write(attempt(1, self.item), attempt(2, self.item))
        state, _ = self.write()
        self.write(critic(1, self.item, state, self.tier))
        state, rejected = self.write(escalate(1, self.item))
        self.assertEqual(rejected, ["es-1"])
        record = blockers.record(blockers.find(state, self.item))
        self.assertEqual(blockers.next_step(record), blockers.ATTEMPT)

    def test_at_full_the_full_record_lands_with_its_comment(self):
        self.full_record()
        state, rejected = self.write(escalate(1, self.item))
        self.assertEqual(rejected, [])
        (row,) = self.manual_priorities(state)
        self.assertEqual((row["item"], row["by"]), (self.item, BOSS))
        record = blockers.record(blockers.find(state, self.item))
        self.assertEqual([a["after_critic"] for a in record["attempts"]], [False, False, True])
        self.assertEqual(blockers.next_step(record), blockers.ESCALATED)
        (comment,) = [c for c in blockers.find(state, self.item)["comments"] if c["by"] == BOSS]
        for line in (
            "restarted the runner",
            "drained the node",
            "scaled the pool to four",
            "kubectl rollout restart deployment/runner",
            "deployment.apps/runner restarted",
            "Scale the runner pool before restarting it.",
            "The runner pool quota is spent until the weekly reset.",
            "gh api rate_limit shows zero remaining",
        ):
            self.assertIn(line, comment["text"])

    def test_after_the_escalation_a_priority_on_the_item_lands(self):
        self.full_record()
        self.write(escalate(1, self.item))
        _, rejected = self.write(priority(1, self.item))
        self.assertEqual(rejected, [])

    def test_the_swarm_and_the_operator_are_not_gated(self):
        swarm = {**priority(1, self.item), "by": "swarm"}
        operator = {"op": "priority_clear", "id": "c1", "target": "all"}
        _, rejected = self.write(swarm, operator)
        self.assertEqual(rejected, [])

    def test_one_attempt_applies_the_critic_resolution(self):
        self.full_record()
        _, rejected = self.write(attempt(4, self.item, "scaled the pool to eight"))
        self.assertEqual(rejected, ["at-4"])

    def test_a_follow_up_flag_and_a_question_at_full_are_refused(self):
        state, _ = self.write()
        followup = f"followups/{state['followups'][0]['id']}/needs_operator"
        flag = {"op": "set", "id": "s1", "by": BOSS, "path": followup, "value": True}
        question = {"op": "add_item", "id": "q1", "by": BOSS, "list": "questions", "text": "Which pool size?"}
        _, rejected = self.write(flag, question)
        self.assertEqual(rejected, ["s1", "q1"])


class Assist(Protocol):
    tier = "assist"

    def test_at_full_a_priority_with_no_record_is_refused(self):
        state, rejected = self.write(priority(1, self.item))
        self.assertEqual(rejected, [])
        self.assertEqual(len(self.manual_priorities(state)), 1)

    def test_at_full_an_escalation_with_no_record_is_refused(self):
        state, rejected = self.write(escalate(1, self.item))
        self.assertEqual(rejected, [])
        self.assertEqual(len(self.manual_priorities(state)), 1)

    def test_at_full_two_attempts_and_no_critic_are_refused(self):
        self.write(attempt(1, self.item), attempt(2, self.item))
        _, rejected = self.write(escalate(1, self.item), priority(2, self.item))
        self.assertEqual(rejected, [])

    def test_the_critic_without_an_attempt_after_it_is_refused(self):
        self.write(attempt(1, self.item), attempt(2, self.item))
        state, _ = self.write()
        self.write(critic(1, self.item, state, self.tier))
        _, rejected = self.write(escalate(1, self.item))
        self.assertEqual(rejected, [])

    def test_a_follow_up_flag_and_a_question_at_full_are_refused(self):
        state, _ = self.write()
        followup = f"followups/{state['followups'][0]['id']}/needs_operator"
        flag = {"op": "set", "id": "s1", "by": BOSS, "path": followup, "value": True}
        _, rejected = self.write(flag)
        self.assertEqual(rejected, [])


class Steps(unittest.TestCase):
    def test_the_next_step_is_counted_by_code(self):
        empty = blockers.record({})
        self.assertEqual(blockers.next_step(empty), blockers.ATTEMPT)
        one = {"attempts": [{"after_critic": False}], "critic": None, "escalation": None}
        self.assertEqual(blockers.next_step(one), blockers.ATTEMPT)
        two = {**one, "attempts": [{"after_critic": False}] * 2}
        self.assertEqual(blockers.next_step(two), blockers.CRITIC)
        criticized = {**two, "critic": {"resolution": "r"}}
        self.assertEqual(blockers.next_step(criticized), blockers.ATTEMPT)
        tried = {**criticized, "attempts": [*two["attempts"], {"after_critic": True}]}
        self.assertEqual(blockers.next_step(tried), blockers.ESCALATE)
        self.assertEqual(blockers.next_step({**tried, "escalation": {"reason": "r"}}), blockers.ESCALATED)

    def test_the_blind_prompt_holds_the_attempts_and_never_the_conclusion(self):
        attempts = [{"text": "restarted it", "command": "systemctl restart x", "output": "ok", "after_critic": False}]
        prompt = blockers.blind_prompt("tasks/t1", "Deploy the hive", "full", attempts)
        for part in ("tasks/t1", "Deploy the hive", "full", "restarted it", "systemctl restart x"):
            self.assertIn(part, prompt)
        self.assertNotIn("escalat", prompt.lower())

    def test_the_delivery_notice_names_the_tier_and_the_next_step(self):
        notice = blockers.notice("sw", "full", "tasks/t1", blockers.record({}))
        self.assertIn("Autonomy full", notice)
        self.assertIn("agentihooks swarm sw resolve attempt tasks/t1", notice)
        assist = blockers.notice("sw", "assist", "tasks/t1", blockers.record({}))
        self.assertIn("Autonomy assist", assist)
        self.assertNotIn("resolve attempt", assist)


if __name__ == "__main__":
    unittest.main()
