import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PRELOAD = ROOT / "tests/node_capture.cjs"
CONVERT = ROOT / "tests/js_lcov.py"

PAGE = """import { helper } from "./dom.js";

// Totals the rows the panel shows.
export function total(rows) {
  let sum = 0;
  for (const row of rows) {
    sum += row;
  }
  return sum;
}

function unused(rows) {
  const doubled = rows.map((row) => row * 2);
  return doubled.length;
}

export function neverExtracted(value) {
  return value + 1;
}
"""


def node(script, captures):
    env = {**os.environ, "NODE_OPTIONS": f"--require {PRELOAD}", "JS_COVERAGE_DIR": str(captures)}
    return subprocess.run(["node", "-e", script], env=env, check=True, capture_output=True, text=True).stdout


def extracted(source, name):
    body = []
    for line in source.splitlines():
        if not line.startswith("import "):
            body.append(("  " + line.removeprefix("export ")) if line else "")
    text = "\n".join(body)
    return f"function {name}(" + text.split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def convert(root, captures, out):
    return subprocess.run(
        [sys.executable, str(CONVERT), "--root", str(root), "--captures", str(captures), "--out", str(out)],
        capture_output=True,
        text=True,
    )


def records(lcov):
    files, current = {}, None
    for line in lcov.read_text().splitlines():
        if line.startswith("SF:"):
            current = files.setdefault(line[3:], {})
        elif line.startswith("DA:"):
            number, hits = line[3:].split(",")
            current[int(number)] = int(hits)
    return files


def numbered(text):
    return {line: n for n, line in enumerate(PAGE.splitlines(), 1) if line.strip() == text}


def stand_in(captures, kind):
    """A recorded run of the other kind, since the report refuses captures missing either kind."""
    (captures / "sources").mkdir(parents=True, exist_ok=True)
    (captures / "sources" / "stand-in.js").write_text("void 0;\n")
    entry = {"source": "stand-in", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 7, "count": 1}]}]}
    (captures / f"capture-{kind}-stand-in.json").write_text(json.dumps({"result": [entry]}))


def test_a_node_eval_leaves_its_script_and_its_precise_coverage(tmp_path):
    script = "function a(x) { return x ? 1 : 2; }\nprocess.stdout.write(String(a(0)));"
    assert node(script, tmp_path) == "2"
    (capture,) = tmp_path.glob("capture-node-*.json")
    (evaluated,) = json.loads(capture.read_text())["result"]
    assert (tmp_path / "sources" / f"{evaluated['source']}.js").read_text() == script
    assert {fn["functionName"] for fn in evaluated["functions"]} >= {"", "a"}


def test_a_node_run_without_a_coverage_folder_leaves_nothing(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "JS_COVERAGE_DIR"}
    env["NODE_OPTIONS"] = f"--require {PRELOAD}"
    done = subprocess.run(["node", "-e", "1"], env=env, cwd=tmp_path, check=True, capture_output=True, text=True)
    assert done.stdout == done.stderr == ""
    assert list(tmp_path.rglob("capture-*.json")) == []


def test_lines_a_test_ran_count_and_every_other_executable_line_counts_zero(tmp_path):
    page = tmp_path / "scripts/swarm_ledger/static/js/panel.js"
    page.parent.mkdir(parents=True)
    page.write_text(PAGE)
    captures = tmp_path / "captures"
    script = (
        extracted(PAGE, "total") + "\n" + extracted(PAGE, "unused") + "\nprocess.stdout.write(String(total([1, 2])));"
    )
    assert node(script, captures) == "3"

    stand_in(captures, "browser")
    result = convert(tmp_path, captures, tmp_path / "lcov.info")
    assert result.returncode == 0, result.stdout + result.stderr
    hits = records(tmp_path / "lcov.info")["scripts/swarm_ledger/static/js/panel.js"]

    for text in ("export function total(rows) {", "let sum = 0;", "sum += row;", "return sum;"):
        (line,) = numbered(text).values()
        assert hits[line] > 0, text
    for text in ("const doubled = rows.map((row) => row * 2);", "return doubled.length;", "return value + 1;"):
        (line,) = numbered(text).values()
        assert hits[line] == 0, text
    skipped = [
        n for n, line in enumerate(PAGE.splitlines(), 1) if not line.strip() or line.startswith(("import", "//"))
    ]
    assert skipped and not set(skipped) & set(hits)


def test_two_scripts_that_ran_the_same_line_add_their_counts(tmp_path):
    page = tmp_path / "scripts/swarm_ledger/static/js/panel.js"
    page.parent.mkdir(parents=True)
    page.write_text(PAGE)
    captures = tmp_path / "captures"
    for _ in range(2):
        node(extracted(PAGE, "total") + "\ntotal([1]);", captures)
    stand_in(captures, "browser")
    assert convert(tmp_path, captures, tmp_path / "lcov.info").returncode == 0
    (line,) = numbered("return sum;").values()
    assert records(tmp_path / "lcov.info")["scripts/swarm_ledger/static/js/panel.js"][line] == 2


@pytest.mark.parametrize(
    "recorded, missing",
    [([], "browser or node"), (["node"], "browser"), (["browser"], "node")],
    ids=["nothing", "no-browser", "no-node"],
)
def test_a_report_missing_either_kind_of_recorded_run_is_red(tmp_path, recorded, missing):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/page.js").write_text("export const a = 1;\n")
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / "capture-browser-empty.json").write_text('{"result": []}')
    for kind in recorded:
        stand_in(captures, kind)
    result = convert(tmp_path, captures, tmp_path / "lcov.info")
    assert result.returncode != 0
    assert f"No {missing} coverage recorded" in result.stdout
    assert not (tmp_path / "lcov.info").exists()


def test_code_two_page_files_share_counts_in_both(tmp_path):
    for name in ("a", "b"):
        page = tmp_path / f"scripts/swarm_ledger/static/js/{name}.js"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(PAGE)
    captures = tmp_path / "captures"
    node(extracted(PAGE, "total") + "\ntotal([1]);", captures)
    stand_in(captures, "browser")
    assert convert(tmp_path, captures, tmp_path / "lcov.info").returncode == 0
    (line,) = numbered("return sum;").values()
    files = records(tmp_path / "lcov.info")
    assert files["scripts/swarm_ledger/static/js/a.js"][line] == files["scripts/swarm_ledger/static/js/b.js"][line] == 1


def test_the_coverage_shards_capture_node_runs_and_sonar_imports_the_report():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["unit"]["steps"]
    run = next(step for step in steps if step.get("name") == "Run tests")
    assert "matrix.python-version == '3.12'" in run["env"]["NODE_OPTIONS"]
    assert "tests/node_capture.cjs" in run["env"]["NODE_OPTIONS"]
    assert "matrix.python-version == '3.12'" in run["env"]["JS_COVERAGE_DIR"]
    assert "-p tests.browser_coverage" in run["env"]["PYTEST_ADDOPTS"].split("||")[0]
    assert run["env"]["PLAYWRIGHT_BROWSERS_PATH"] == (
        "${{ matrix.python-version == '3.12' && format('{0}/.playwright', github.workspace) || '' }}"
    )
    names = [step.get("name") for step in steps]
    browser = steps[names.index("Install the browser that page tests drive")]
    assert browser["uses"] == "./.github/actions/browser-cache"
    assert browser["if"] == "matrix.python-version == '3.12'"
    assert names.index("Install the browser that page tests drive") < names.index("Run tests")
    upload = next(step for step in steps if step.get("name") == "Upload coverage")
    assert "js-coverage/" in upload["with"]["path"].split()
    combine = (ROOT / ".github/coverage/combine.sh").read_text()
    assert "js_lcov.py" in combine and "--out lcov.info" in combine
    evidence = next(
        step
        for step in workflow["jobs"]["sonar"]["steps"]
        if step.get("name") == "Upload coverage and analysis evidence"
    )
    assert "lcov.info" in evidence["with"]["path"].split()
    properties = (ROOT / "sonar-project.properties").read_text().splitlines()
    assert "sonar.javascript.lcov.reportPaths=lcov.info" in properties


def test_a_browser_page_leaves_the_scripts_it_ran_and_their_lines_count(tmp_path, monkeypatch):
    from tests import browser_coverage
    from tests.swarm_ledger.ledger_page import chromium

    captures = tmp_path / "captures"
    for owner, name, replacement in browser_coverage.patches(captures):
        monkeypatch.setattr(owner, name, replacement)
    page_file = tmp_path / "scripts/swarm_ledger/static/js/panel.js"
    page_file.parent.mkdir(parents=True)
    page_file.write_text(PAGE)
    assets = {"/js/panel.js": PAGE, "/js/dom.js": "export const helper = 1;\n"}
    with chromium() as browser:
        tab = browser.new_page()
        tab.route(
            "http://127.0.0.1:9/**",
            lambda route: route.fulfill(
                body=assets.get(route.request.url[len("http://127.0.0.1:9") :], "<html></html>"),
                content_type="text/javascript" if route.request.url.endswith(".js") else "text/html",
            ),
        )
        tab.goto("http://127.0.0.1:9/ledger")
        assert tab.evaluate("async () => (await import('/js/panel.js')).total([1, 2])") == 3
    assert list(captures.glob("capture-browser-*.json"))

    stand_in(captures, "node")
    assert convert(tmp_path, captures, tmp_path / "lcov.info").returncode == 0
    hits = records(tmp_path / "lcov.info")["scripts/swarm_ledger/static/js/panel.js"]
    for text in ("let sum = 0;", "return sum;"):
        (line,) = numbered(text).values()
        assert hits[line] > 0, text
    for text in ("return doubled.length;", "return value + 1;"):
        (line,) = numbered(text).values()
        assert hits[line] == 0, text
