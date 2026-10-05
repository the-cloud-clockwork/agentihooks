import json
import re
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"


def page():
    return (SCRIPTS / "template.html").read_text(encoding="utf-8")


def function_source(name):
    return f"function {name}(" + page().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def render(task):
    stubs = (
        "const openComments = new Set(); const closedComments = new Set();"
        "const h = (tag, attrs, ...kids) => ({ tag, attrs: attrs || {}, kids: kids.filter(Boolean), addEventListener() {} });"
        "const scopeDot = () => null; const commentsView = (key) => ({ tag: 'comments', attrs: { key }, kids: [] });"
        "const ser = (n) => (n && typeof n === 'object' ? [n.tag, n.attrs.class || '', n.attrs.text || '', n.kids.map(ser)] : n);"
    )
    script = (
        stubs
        + "".join(function_source(n) + "\n" for n in ("itemClass", "taskBlockers", "taskProof", "taskRow"))
        + f"const t = {json.dumps(task)}; const el = taskRow(t, [t]);"
        + "const proof = (function find(n) { if (!n || typeof n !== 'object') return null;"
        + " if (n.tag === 'details') return n; for (const k of n.kids) { const f = find(k); if (f) return f; } return null; })(el);"
        + "process.stdout.write(JSON.stringify({ tree: ser(el), proof: proof && { comments_key: proof.attrs['data-key'] || null, open: !!proof.open } }));"
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
                                    ["div", "item-title", "Build the inbox", []],
                                    ["p", "desc", "d", []],
                                    [
                                        "div",
                                        "task-meta",
                                        "",
                                        [["span", "", "eng", []], ["span", "", "open", []], ["span", "", "p1", []]],
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


if __name__ == "__main__":
    unittest.main()
