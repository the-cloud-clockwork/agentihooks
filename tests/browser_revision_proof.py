import json
import os
import pwd
import subprocess
import sys


def test_setup_and_isolated_home_use_the_same_playwright():
    probe = """
import json
import site
import sys
from importlib.metadata import version
import playwright
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    result = {"python": sys.executable, "version": version("playwright"), "module": playwright.__file__, "user_site": site.getusersitepackages(), "expected": pw.chromium.executable_path}
    try:
        pw.chromium.launch().close()
        result["launch"] = "success"
    except Exception as exc:
        result["launch"] = str(exc).splitlines()[0]
    print(json.dumps(result))
"""
    outputs = {}
    for name, env in (
        ("runner_home", {**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir}),
        ("isolated_home", dict(os.environ)),
        ("runner_without_user_site", {**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir, "PYTHONNOUSERSITE": "1"}),
    ):
        result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True)
        outputs[name] = json.loads(result.stdout)
    print("[BROWSER-REVISION] " + json.dumps(outputs), flush=True)
    assert outputs["runner_home"]["version"] == outputs["isolated_home"]["version"], outputs
