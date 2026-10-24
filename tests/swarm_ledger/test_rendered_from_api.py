import json
import re
import subprocess
import urllib.request

import pytest

from scripts.swarm import phase_state
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_server as server
from tests.swarm_ledger.ledger_page import MODULES, ledger_state, served, shell_html, show
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
HOME_JS = MODULES.parent / "home" / "home.js"


def phase(pid, title, **extra):
    return {"id": pid, "title": title, "description": "", "done": False, "depends_on": [], **extra}


PHASES = [
    phase("a", "Alpha", done=True),
    phase("b", "Bravo", depends_on=["a"], planning="auto"),
    phase("c", "Charlie", depends_on=["b", "a"]),
    phase("d", "Delta", out_of_scope=True),
]


def test_the_served_view_carries_each_phase_status_and_label():
    view = server.page_view({"phases": PHASES, "tasks": [], "_meta": {}}, *server.NO_DOCTOR)
    assert view["phase_status"] == {
        "a": {"state": "done", "label": "Done"},
        "b": {"state": "to_plan", "label": "Needs a plan"},
        "c": {"state": "waiting", "label": "Waits for phase Bravo"},
        "d": {"state": "out_of_scope", "label": "Out of scope"},
    }


def test_a_waiting_label_names_every_unfinished_dependency_by_title_or_id():
    doc = {"phases": [phase("x", "", depends_on=["gone", "y"]), phase("y", "Yankee")], "tasks": []}
    assert phase_state.label(doc["phases"][0], doc) == "Waits for phase gone, Yankee"
    assert phase_state.label(doc["phases"][1], doc) == phase_state.LABELS["building"] == "Building"


def test_a_ledger_without_a_tasks_list_still_serves_each_phase_status():
    view = server.page_view({"phases": PHASES, "_meta": {}}, *server.NO_DOCTOR)
    assert view["phase_status"]["b"] == {"state": "to_plan", "label": "Needs a plan"}


def test_a_waiting_label_without_a_phases_list_names_the_dependency_id():
    assert phase_state.label(phase("x", "", depends_on=["gone"]), {"tasks": []}) == "Waits for phase gone"


def test_the_page_renders_the_served_phase_status_and_keeps_no_lifecycle_of_its_own():
    state, render = ((MODULES / name).read_text(encoding="utf-8") for name in ("state.js", "render.js"))
    assert "phaseLifecycle" not in state + render
    assert "PHASE_LABELS" not in (MODULES / "config.js").read_text(encoding="utf-8") + render
    review = (MODULES / "phase_review.js").read_text(encoding="utf-8")
    assert "phaseLifecycle" not in review
    assert "phase_status" in review and "phaseStatus(item).label" in render


def apply_ops(doc, ops):
    source = (MODULES / "state.js").read_text(encoding="utf-8")
    body = "\n".join(line.removeprefix("export ") for line in source.splitlines() if not line.startswith("import "))
    probe = (
        "const LS_KEY = 'k', checks = {}, doc = null, ops = [];\n"
        "function setFreeze() {}\nfunction clearFreeze() {}\n"
        + body
        + f"\nconst d = withDefaults({json.dumps(doc)});\n"
        + f"for (const op of {json.dumps(ops)}) applyOp(d, op);\n"
        + "process.stdout.write(JSON.stringify(d));\n"
    )
    return json.loads(subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True).stdout)


DOC = {
    "phases": [phase("p1", "Plan", planning="auto", review={"state": "pending"})],
    "tasks": [{"id": "t1", "title": "T", "rank": "normal", "comments": []}],
    "priorities": [{"id": "pr1", "item": "tasks/t1", "text": "approve"}],
    "alerts": [{"id": "a1", "text": "too big", "state": "open"}],
    "artifacts": [{"id": "x1", "title": "Plan", "file": {"id": "f1"}}],
}
OPS = [
    {"op": "verdict", "id": "c1", "item": "tasks/t1", "verdict": "approved"},
    {"op": "phase_review", "id": "r1", "item": "phases/p1", "state": "approved"},
    {"op": "task_rank", "id": "k1", "item": "tasks/t1", "rank": "next"},
    {"op": "alert_claim", "id": "l1", "target": "a1"},
    {"op": "artifact_delete", "id": "d1", "target": "x1"},
    {"op": "priority_clear", "id": "q1", "target": "all"},
]


def test_an_action_the_server_decides_is_marked_pending_instead_of_replayed():
    d = apply_ops(DOC, OPS)
    assert d["priorities"] == DOC["priorities"]
    assert d["phases"][0]["review"] == {"state": "pending"}
    assert d["tasks"][0]["rank"] == "normal"
    assert d["alerts"][0]["state"] == "open"
    assert [a["id"] for a in d["artifacts"]] == ["x1"] and d["artifact_trash"] == []
    assert {key: op["op"] for key, op in d["pending"].items()} == {
        "tasks/t1": "task_rank",
        "phases/p1": "phase_review",
        "alerts/a1": "alert_claim",
        "artifacts/x1": "artifact_delete",
        "priorities/all": "priority_clear",
    }
    assert [c["text"] for c in d["tasks"][0]["comments"]] == ["approved"]


def test_a_served_view_starts_with_nothing_pending():
    assert apply_ops(DOC, [])["pending"] == {}


def test_home_and_bin_are_one_static_shell():
    home = server.index_page()
    assert "__HOME_" not in home
    assert set(re.findall(r"__HOME_(\w+)__", core.HOME.read_text(encoding="utf-8"))) == {"PAGE"}
    assert "<button" not in home and 'class="fab"' not in home and "<header" not in home
    with served(server) as base:
        pages = [urllib.request.urlopen(base + path, timeout=5).read().decode() for path in ("/", "/?view=bin")]
    assert pages[0] == pages[1]


def test_the_home_script_reads_and_writes_only_through_the_shared_api_module():
    script = HOME_JS.read_text(encoding="utf-8")
    assert re.search(r'^import \{[^}]+\} from "\.\./js/api\.js";$', script, re.M)
    assert "fetch(" not in script
    assert "/api/" not in script


ALLOWED_FOLDS = """() => [...document.querySelectorAll("details")].filter((box) => !(
  (box.matches(".col > section > details.fold")) ||
  (box.matches("details.sw-fold")) ||
  (box.closest("#outline") && box.matches("details.fold")) ||
  box.matches("details[data-key]")
)).map((box) => box.className || box.id || box.outerHTML.slice(0, 80))"""


def folding_doc():
    task = {"lane": "eng", "phase": "p1", "comments": [], "contract": {"must": "it works"}, "workspace": ""}
    return ledger_state(
        {
            "title": "Folds",
            "overview": "",
            "plans": [{"id": "pl", "title": "Plan one"}],
            "phases": [phase("p1", "One", plan="plans/pl", comments=[])],
            "tasks": [
                {**task, "id": "t1", "title": "Open", "state": "open"},
                {**task, "id": "t2", "title": "Done", "state": "done", "done": True},
            ],
            "followups": [{"id": "f1", "text": "Closed", "done": True, "comments": []}],
        }
    )


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.set_default_timeout(5000)
    yield page
    context.close()


def test_only_the_folds_the_design_record_allows_render(tab):
    show(tab, shell_html(), ledger=folding_doc())
    tab.click("#tasks-box > summary")
    tab.locator("#item-tasks-t1").wait_for()
    assert tab.evaluate(ALLOWED_FOLDS) == []
    tab.evaluate(
        """() => document.querySelector("#item-phases-p1").append(Object.assign(document.createElement("details"), {className: "planted"}))"""
    )
    assert tab.evaluate(ALLOWED_FOLDS) == ["planted"]
