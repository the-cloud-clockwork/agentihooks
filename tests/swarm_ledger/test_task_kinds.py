import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_kinds  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "kinds-2026-01-01"
CONTRACT = {"must": "the cache hit rate is above ninety percent", "check": "the metrics query", "judge": "master"}


def make_ledger(tasks=()):
    content = {"title": "Demo", "overview": "o", "sources": [str(SCRIPTS)], "phases": [{"title": "one"}]}
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    return core.sync(SLUG, ops=[op("task_add", n, **task) for n, task in enumerate(tasks)])[0]


def op(kind, n, /, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": "swarm", **fields}


def done(n, task="t1", **proof):
    fields = {"state": "done", **({"proof": proof} if proof else {})}
    return op("task_update", n, item=f"tasks/{task}", fields=fields)


class Kinds(unittest.TestCase):
    def test_a_task_without_a_kind_is_code_and_closes_as_before(self):
        make_ledger([{"task": "t1", "title": "a", "lane": "eng"}])
        state, rejected = core.sync(SLUG, ops=[done(1)])
        self.assertEqual(rejected, [])
        self.assertEqual(ledger_kinds.kind(state["tasks"][0]), "code")
        self.assertEqual(state["tasks"][0]["state"], "done")

    def test_task_add_stores_the_kind_and_the_contract(self):
        add = op("task_add", 1, task="t1", title="raise the cache size", lane="eng", kind="tune", contract=CONTRACT)
        core.check_op(add)
        state = make_ledger([{k: v for k, v in add.items() if k not in ("op", "id", "by")}])
        self.assertEqual((state["tasks"][0]["kind"], state["tasks"][0]["contract"]), ("tune", CONTRACT))

    def test_an_unknown_kind_or_a_malformed_contract_is_refused(self):
        for bad in (
            op("task_add", 1, task="t1", title="a", lane="eng", kind="deploy"),
            op("task_add", 2, task="t1", title="a", lane="eng", contract={"must": 1}),
            op("task_add", 3, task="t1", title="a", lane="eng", contract={"who": "x"}),
            op("task_update", 4, item="tasks/t1", fields={"kind": "deploy"}),
            op("task_update", 5, item="tasks/t1", fields={"proof": "ran it"}),
        ):
            with self.assertRaises(ValueError):
                core.check_op(bad)

    def test_an_ops_task_cannot_be_done_without_command_evidence(self):
        make_ledger([{"task": "t1", "title": "restart the cache", "lane": "eng", "kind": "ops"}])
        for n, proof in enumerate(({}, {"command": "kubectl get pods"}, {"output": "Running"}), 1):
            state, rejected = core.sync(SLUG, ops=[done(n, **proof)])
            self.assertEqual(rejected, [f"task_update-{n}"])
            self.assertEqual(state["tasks"][0]["state"], "open")
        state, rejected = core.sync(SLUG, ops=[done(9, command="kubectl get pods", output="cache-0 Running")])
        self.assertEqual(rejected, [])
        self.assertEqual(state["tasks"][0]["proof"], {"command": "kubectl get pods", "output": "cache-0 Running"})
        self.assertTrue(state["tasks"][0]["done"])

    def test_each_kind_names_the_proof_it_needs(self):
        unmet = ledger_kinds.unmet
        self.assertEqual(unmet({}), [])
        self.assertEqual(unmet({"kind": "ci"}), [])
        self.assertEqual(unmet({"kind": "tune", "proof": {"command": "x"}}), ["output"])
        self.assertEqual(unmet({"kind": "troubleshoot"}), ["root_cause", "evidence", "fix or filed"])
        self.assertEqual(
            unmet({"kind": "troubleshoot", "proof": {"root_cause": "r", "evidence": "e", "filed": "t9"}}), []
        )
        self.assertEqual(unmet({"kind": "research"}), ["finding"])
        self.assertEqual(unmet({"kind": "research", "proof": {"finding": "notes in my head"}}), ["finding"])
        self.assertEqual(unmet({"kind": "research", "proof": {"finding": "https://github.com/o/r/pull/1"}}), [])

    def test_a_seed_task_marked_done_without_its_proof_is_refused(self):
        base = {"title": "t", "phases": [], "questions": [], "followups": []}
        with self.assertRaises(ValueError):
            core.validate({**base, "tasks": [{"id": "t1", "title": "a", "kind": "ops", "state": "done"}]})
        core.validate({**base, "tasks": [{"id": "t1", "title": "a", "state": "done"}]})

    def test_an_existing_ledger_without_kinds_loads_unchanged(self):
        make_ledger([{"task": "t1", "title": "a", "lane": "eng"}, {"task": "t2", "title": "b", "lane": "ci"}])
        core.sync(SLUG, ops=[done(1)])
        from scripts.swarm_ledger.repository import repository

        before = repository.get_document(SLUG)
        for task in before["tasks"]:
            task.pop("kind", None)
        repository.import_document(SLUG, before, replace=True)
        after = core.sync(SLUG)[0]
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["_meta"]["rev"], before["_meta"]["rev"])


class KindCli(unittest.TestCase):
    def test_task_add_sends_the_kind_and_the_contract(self):
        import ledger

        sent = []
        argv = ["--slug", SLUG, "--as", "liaison", "task", "add", "t3", "trim", "the", "cache", "--kind", "tune"]
        argv += ["--must", CONTRACT["must"], "--check", CONTRACT["check"], "--judge", CONTRACT["judge"]]
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, /, **f: sent.append((kind, f))):
            ledger.cmd_task(ledger.build_parser().parse_args(argv))
            ledger.cmd_task(
                ledger.build_parser().parse_args(["--slug", SLUG, "--as", "x", "task", "set", "t3", "kind=ops"])
            )
        self.assertEqual((sent[0][1]["kind"], sent[0][1]["contract"]), ("tune", CONTRACT))
        self.assertEqual(sent[1][1]["fields"], {"kind": "ops"})

    def test_task_set_sends_a_dotted_proof_as_one_object_the_ledger_stores(self):
        import ledger

        make_ledger([{"task": "t1", "title": "restart the cache", "lane": "eng", "kind": "ops"}])
        sent = []
        argv = ["--slug", SLUG, "--as", "x", "task", "set", "t1", "state=done"]
        argv += ["proof.command=kubectl get pods", "proof.output=cache-0 Running=1/1"]
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, /, **f: sent.append((kind, f))):
            ledger.cmd_task(ledger.build_parser().parse_args(argv))
        fields = sent[0][1]["fields"]
        self.assertEqual(
            fields, {"state": "done", "proof": {"command": "kubectl get pods", "output": "cache-0 Running=1/1"}}
        )
        state, rejected = core.sync(SLUG, ops=[op("task_update", 1, item="tasks/t1", fields=fields)])
        self.assertEqual(rejected, [])
        self.assertEqual(state["tasks"][0]["proof"], fields["proof"])
        self.assertTrue(state["tasks"][0]["done"])

    def test_task_set_keeps_plain_values_as_strings_for_a_code_task(self):
        import ledger

        sent = []
        argv = ["--slug", SLUG, "--as", "x", "task", "set", "t1", "state=pr", "pr_url=https://github.com/o/r/pull/1"]
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, /, **f: sent.append((kind, f))):
            ledger.cmd_task(ledger.build_parser().parse_args(argv))
        self.assertEqual(sent[0][1]["fields"], {"state": "pr", "pr_url": "https://github.com/o/r/pull/1"})

    def test_task_set_refuses_a_plain_and_a_dotted_proof_together(self):
        import ledger

        sent = []
        argv = ["--slug", SLUG, "--as", "x", "task", "set", "t1", "proof=ran it", "proof.command=kubectl get pods"]
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, /, **f: sent.append((kind, f))):
            with self.assertRaises(SystemExit) as exit_:
                ledger.cmd_task(ledger.build_parser().parse_args(argv))
        self.assertIn("not both", str(exit_.exception.code))
        self.assertEqual(sent, [])
