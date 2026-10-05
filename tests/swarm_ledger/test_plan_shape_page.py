import json
import subprocess

import pytest

from tests.swarm_ledger.test_swarm_panel import function_source

pytestmark = pytest.mark.unit


def test_page_displays_the_report_and_warning_from_status():
    source = function_source("renderPlanShape")
    shape = {
        "summary": "Critical path: 7 tasks\nParallel width: 2 tasks; engineer width: 2",
        "warning": "engineer width 2 is below engineer cap 4",
    }
    code = f"const node = {{}}; const $ = () => node; {source}; renderPlanShape({json.dumps(shape)}); process.stdout.write(JSON.stringify(node));"
    done = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True)
    node = json.loads(done.stdout)
    assert node["textContent"] == shape["summary"] + "\nWarning: " + shape["warning"]
    assert node["hidden"] is False


def test_page_hides_missing_report_and_omits_empty_warning():
    source = function_source("renderPlanShape")
    code = f"const node = {{}}; const $ = () => node; {source}; renderPlanShape(null); const hidden = node.hidden; renderPlanShape({{summary: 'report', warning: ''}}); process.stdout.write(JSON.stringify({{hidden, node}}));"
    done = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True)
    result = json.loads(done.stdout)
    assert result == {"hidden": True, "node": {"hidden": False, "textContent": "report"}}
