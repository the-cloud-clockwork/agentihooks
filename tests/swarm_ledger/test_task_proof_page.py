import json
import re
import subprocess
import unittest
from pathlib import Path

from tests.swarm_ledger.ledger_page import page_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"


def page():
    return page_source()


def function_source(name):
    return f"function {name}(" + page().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def render(task, tail=None):
    stubs = (
        "const openComments = new Set(); const closedComments = new Set();"
        "const h = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.filter(Boolean), addEventListener() {},"
        " replaceWith(node) { Object.assign(this, node); } });"
        "const itemActions = () => null; const commentsView = (key) => ({ tag: 'comments', attrs: { key }, kids: [] });"
        "const doc = { freezes: [] }; const frozen = () => false; const holdClass = () => ''; const holdMark = () => null;"
        "const ser = (n) => (n && typeof n === 'object' ? [n.tag, n.attrs.class || '', n.attrs.text || '', n.kids.map(ser)] : n);"
        "const lazy = (box, fill) => { box.kids.push(...[fill()].flat().filter(Boolean)); return box; };"
        f"const readWorkspace = () => Promise.resolve({{ ok: {json.dumps(tail is not None)},"
        f" json: () => Promise.resolve({{ data: {json.dumps(tail or {})} }}) }});"
    )
    script = (
        stubs
        + "".join(
            function_source(n) + "\n"
            for n in (
                "itemClass",
                "taskBlockers",
                "proofRows",
                "proofList",
                "proofBody",
                "taskProof",
                "taskLink",
                "taskRanks",
                "taskRank",
                "rankPick",
                "difficultyLabel",
                "taskRow",
            )
        )
        + f"const t = {json.dumps(task)}; const el = taskRow(t, [t]);"
        + "setTimeout(() => {"
        + " const proof = (function find(n) { if (!n || typeof n !== 'object') return null;"
        + " if (n.tag === 'details') return n; for (const k of n.kids) { const f = find(k); if (f) return f; } return null; })(el);"
        + " process.stdout.write(JSON.stringify({ tree: ser(el), proof: proof && { comments_key: proof.attrs['data-key'] || null, open: !!proof.open } }));"
        + " }, 0);"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def texts(node):
    if not isinstance(node, list):
        return [node]
    return [node[2], *(t for kid in node[3] for t in texts(kid))]


def nodes(node, cls):
    if not isinstance(node, list):
        return []
    return ([node] if node[1] == cls else []) + [n for kid in node[3] for n in nodes(kid, cls)]


PLAIN = {"id": "t1", "title": "Build the inbox", "description": "d", "lane": "eng", "state": "open", "phase": "p1"}


class TaskProofOnThePage(unittest.TestCase):
    def test_a_plain_task_renders_unchanged(self):
        self.assertEqual(
            render(PLAIN)["tree"],
            [
                "li",
                "item",
                "",
                [
                    [
                        "div",
                        "row",
                        "",
                        [
                            [
                                "div",
                                "",
                                "",
                                [
                                    [
                                        "div",
                                        "item-title",
                                        "",
                                        [["a", "task-id", "t1", []], ["span", "", "Build the inbox", []]],
                                    ],
                                    ["p", "desc", "d", []],
                                    [
                                        "div",
                                        "task-meta",
                                        "",
                                        [
                                            [
                                                "select",
                                                "rank-pick rank-normal",
                                                "",
                                                [
                                                    ["option", "", "urgent", []],
                                                    ["option", "", "high", []],
                                                    ["option", "", "normal", []],
                                                    ["option", "", "low", []],
                                                ],
                                            ],
                                            ["span", "", "eng", []],
                                            ["span", "", "open", []],
                                            ["span", "", "p1", []],
                                        ],
                                    ],
                                ],
                            ]
                        ],
                    ],
                    ["comments", "", "", []],
                ],
            ],
        )

    def test_a_task_with_kind_contract_and_proof_renders_all_three(self):
        task = {
            **PLAIN,
            "kind": "ops",
            "contract": {"must": "the pod is ready", "check": "kubectl get pod", "judge": "the master"},
            "proof": {
                "command": "kubectl get pod web",
                "output": "web 1/1 Running",
                "finding": "https://example.com/f",
            },
        }
        out = render(task)
        self.assertEqual([n[2] for n in nodes(out["tree"], "kind")], ["ops"])
        self.assertEqual(out["proof"], {"comments_key": None, "open": False})
        shown = texts(out["tree"])
        for text in (
            "Contract and proof",
            "Must",
            "the pod is ready",
            "Check",
            "kubectl get pod",
            "Judge",
            "the master",
        ):
            self.assertIn(text, shown)
        for text in ("Command", "kubectl get pod web", "Output", "web 1/1 Running", "Finding", "https://example.com/f"):
            self.assertIn(text, shown)

    def test_a_kind_alone_shows_the_label_and_no_proof_dropdown(self):
        out = render({**PLAIN, "kind": "code"})
        self.assertEqual([n[2] for n in nodes(out["tree"], "kind")], ["code"])
        self.assertIsNone(out["proof"])

    def test_a_proof_dropdown_keeps_its_open_state_across_renders(self):
        source = function_source("taskProof")
        self.assertIn("openComments.has(key)", source)
        self.assertIn('addEventListener("toggle"', source)

    def test_show_and_hide_all_comments_leave_the_proof_dropdown_alone(self):
        self.assertIn("commentBoxes(section)", function_source("setAllComments"))
        self.assertIn('section.querySelectorAll("details[data-key]")', function_source("commentBoxes"))
        self.assertNotIn("data-key", function_source("taskProof"))

    def test_proof_styles_use_only_palette_tokens(self):
        for selector in (r"\.task-meta \.kind", r"\.task-proof dt", r"\.task-proof dd"):
            rule = re.search(rf"(?m)^{selector}[^{{]*\{{([^}}]*)\}}", page()).group(1)
            self.assertNotRegex(rule, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule)
            self.assertNotRegex(rule, r"(?<![-\w])(background|border):", rule)


def blockers(tasks):
    script = (
        function_source("taskBlockers")
        + f"\nconst ts = {json.dumps(tasks)};\nprocess.stdout.write(JSON.stringify(ts.map((t) => taskBlockers(t, ts))));"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


INBOX = {"id": "t1", "title": "Build the inbox", "state": "claimed", "branch": "engineer-a1-0001"}
TICK = {"id": "t2", "title": "Speed up the tick", "state": "pr", "branch": "engineer-a1-0002"}


class TaskBlockersMirrorTheClaimRule(unittest.TestCase):
    def test_a_task_on_branched_dependencies_reads_ready_to_start_on_their_branches(self):
        task = {"id": "t3", "title": "Deliver messages", "state": "open", "depends_on": ["t1", "t2"]}
        self.assertEqual(
            blockers([INBOX, TICK, task])[2],
            "Ready to start on branch engineer-a1-0001 of Build the inbox, and branch engineer-a1-0002 of Speed up the tick",
        )

    def test_a_parked_task_reads_parked_on_its_branch_until_its_blocker_is_done(self):
        parked = {
            "id": "t3",
            "title": "Deliver messages",
            "state": "open",
            "branch": "engineer-a1-0003",
            "depends_on": ["t1"],
            "parked_on": ["t1"],
        }
        bare = {**parked, "id": "t4", "branch": ""}
        finished = {**parked, "id": "t5", "parked_on": ["t6"]}
        merged = {"id": "t6", "title": "Old work", "state": "done"}
        self.assertEqual(
            blockers([INBOX, parked, bare, finished, merged])[1:4],
            [
                "Parked on branch engineer-a1-0003 until Build the inbox is done",
                "Parked on its branch until Build the inbox is done",
                "Ready to start on branch engineer-a1-0001 of Build the inbox",
            ],
        )

    def test_only_dependencies_without_a_branch_hold_a_task(self):
        tasks = [
            INBOX,
            {"id": "t2", "title": "Speed up the tick", "state": "claimed"},
            {"id": "t3", "title": "Write docs", "state": "blocked", "branch": "engineer-a1-0004"},
            {"id": "t4", "title": "Deliver messages", "state": "open", "depends_on": ["t1", "t2", "t3"]},
        ]
        self.assertEqual(blockers(tasks)[3], "Waiting until Speed up the tick is done, and Write docs is done")

    def test_territory_overlap_is_no_waiting_reason(self):
        running = {**INBOX, "territory": ["scripts/swarm"]}
        task = {"id": "t2", "title": "Deliver messages", "state": "open", "territory": ["scripts/swarm/tick.py"]}
        self.assertEqual(blockers([running, task]), ["", ""])


if __name__ == "__main__":
    unittest.main()
