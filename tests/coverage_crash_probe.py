import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

AUDIT = """
import os, sys, traceback
LOG = os.environ["PROBE_AUDIT_LOG"]
def hook(event, args):
    if event in ("os.remove", "os.unlink", "os.rename", "os.replace", "shutil.rmtree", "os.truncate", "open"):
        path = str(args[0]) if args else ""
        if ".coverage" not in path:
            return
        if event == "open" and not (len(args) > 1 and isinstance(args[1], str) and ("w" in args[1])):
            return
        with open(LOG, "a") as out:
            out.write(f"{os.getpid()} {event} {args!r} argv={sys.argv[:4]!r}\\n")
            out.write("".join(traceback.format_stack(limit=25)) + "\\n---\\n")
sys.addaudithook(hook)
"""

CASES = """
import os, time, pytest

@pytest.mark.parametrize("n", range(40))
def test_ok(n):
    time.sleep(0.15)

def test_crash(tmp_path_factory):
    flag = tmp_path_factory.getbasetemp().parent.parent / "crashed"
    time.sleep(1)
    if not flag.exists():
        flag.write_text("1")
        os._exit(1)
"""


@pytest.mark.parametrize("variant", ["ci", "no-reruns", "no-plugins"])
def test_one_crashed_worker_costs_one_test(tmp_path, variant):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sitecustomize.py").write_text(AUDIT)
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_cases.py").write_text(CASES)
    log = tmp_path / "audit.log"
    args = ["-n", "3", "--dist", "loadgroup", "-v", "--tb=line", "-p", "no:cacheprovider"]
    args += ["--cov=.", "--cov-report=", "--cov-context=test", f"--basetemp={tmp_path / 'base' / 'b' / 'b'}"]
    if variant != "no-reruns":
        args += ["--reruns", "2"]
    if variant == "ci":
        args += ["-p", "tests.flake_ledger", "-p", "tests.browser_coverage", "-p", "tests.dep_inputs"]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "site"), str(ROOT)])
    env["PROBE_AUDIT_LOG"] = str(log)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "test_cases.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    errors = len(re.findall(r"^\[gw\d+\] .*\bERROR\b", result.stdout, re.M))
    files = sorted(path.name for path in project.glob(".coverage*"))
    report = textwrap.dedent(f"""
        variant={variant} rc={result.returncode} errored lines={errors}
        files={files}
        --- stdout tail
        {result.stdout[-6000:]}
        --- stderr tail
        {result.stderr[-4000:]}
        --- audit
        {log.read_text()[-12000:] if log.exists() else "none"}
    """)
    passed = len(re.findall(r"^\[gw\d+\] .*\bPASSED\b", result.stdout, re.M))
    assert errors <= 1 and passed >= 40 and False, f"passed={passed} {report}"
