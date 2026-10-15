import dis
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest
from coverage import CoverageData

from scripts import account_names
from scripts.swarm_ledger import artifact_sanity


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="Browser coverage runs on Python 3.12")
def test_only_browser_test_files_track_greenlets_and_record_only_executable_artifact_sanity_lines(tmp_path):
    root = Path(__file__).parents[2]
    tests = {
        "shared.py": """
            import threading

            from scripts import account_names

            release = threading.Event()
            thread = threading.Thread(target=lambda: release.wait() and account_names.codex_token_env("a@b"), daemon=True)
        """,
        "test_1_plain.py": """
            import sys

            import shared

            def test_plain_code_runs_without_greenlet_tracking_and_starts_a_thread():
                assert sys.gettrace().concur_id_func is None
                shared.thread.start()
        """,
        "test_2_page.py": """
            import os
            import sys
            from pathlib import Path

            import greenlet
            from playwright.sync_api import sync_playwright

            from scripts.swarm_ledger import artifact_sanity

            def test_page_code_runs_with_greenlet_tracking():
                assert sys.gettrace().concur_id_func is greenlet.getcurrent
                files = sorted(Path(os.environ["ARTIFACT_FIXTURES"]).iterdir())
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    try:
                        assert artifact_sanity.run(browser, files) == {path.name: [] for path in files}
                    finally:
                        browser.close()
        """,
        "test_3_plain.py": """
            import sys

            import shared

            def test_a_thread_started_before_a_page_test_is_still_traced():
                assert sys.gettrace().concur_id_func is None
                shared.release.set()
                shared.thread.join(10)
                assert not shared.thread.is_alive()
        """,
    }
    for name, body in tests.items():
        (tmp_path / name).write_text(textwrap.dedent(body))
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_", "JS_COVERAGE_", "PYTEST_"))
    }
    env |= {
        "PYTHONPATH": os.pathsep.join([str(root), *sys.path]),
        "COVERAGE_FILE": str(tmp_path / ".coverage"),
        "ARTIFACT_FIXTURES": str(root / "tests/fixtures/artifacts"),
    }
    command = [sys.executable, "-m", "pytest", "-p", "tests.browser_coverage", "--cov=scripts", "--cov-report="]
    command += [f"--cov-config={root}/.github/coverage/coverage.ini", "-p", "no:cacheprovider", *sorted(tests)[1:]]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr

    source = root / "scripts/swarm_ledger/artifact_sanity.py"
    pending = [compile(source.read_text(), str(source), "exec")]
    executable = set()
    while pending:
        code = pending.pop()
        executable.update(line for _, line in dis.findlinestarts(code))
        pending.extend(value for value in code.co_consts if isinstance(value, types.CodeType))
    data = CoverageData(str(tmp_path / ".coverage"))
    data.read()
    measured = {Path(name).resolve(): name for name in data.measured_files()}
    threaded = set(data.lines(measured[Path(account_names.__file__).resolve()]) or [])
    assert account_names.codex_token_env.__code__.co_firstlineno + 1 in threaded, threaded
    recorded = set(data.lines(measured[source]) or [])
    assert artifact_sanity.run.__code__.co_firstlineno + 1 in recorded, recorded
    viewer = {line for _, line in dis.findlinestarts(artifact_sanity._view.__code__)}
    assert not (missing := viewer - recorded), sorted(missing)
    assert not (impossible := recorded - executable), sorted(impossible)
