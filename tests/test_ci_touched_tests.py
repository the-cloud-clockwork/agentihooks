import re
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("selected", "maximum", "workers"),
    [(4, 8, 4), (10, 2, 2), (1, 0, 1), (1, None, 1), (4, "plugin", 2), (None, 8, None)],
)
def test_touched_test_workers_follow_the_collected_selection(tmp_path, monkeypatch, selected, maximum, workers):
    from scripts.ci_touched_tests import worker_count

    suite = tmp_path / f"test_selection_{selected}_{maximum}.py"
    suite.write_text(
        "invalid python!\n"
        if selected is None
        else f"import pytest\n@pytest.mark.parametrize('case', range({selected}))\ndef test_case(case):\n    pass\n"
    )
    if maximum == "plugin":
        (tmp_path / "conftest.py").write_text(
            "import pytest\n"
            "def pytest_addoption(parser):\n    parser.addoption('--test-workers', type=int, default=2)\n"
            "@pytest.hookimpl(tryfirst=True)\n"
            "def pytest_xdist_auto_num_workers(config):\n    return config.getoption('--test-workers')\n"
        )
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    if maximum is None or maximum == "plugin":
        monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", str(maximum))
    args = [str(suite), "--tb=short", "-p", "no:cacheprovider"]
    if selected is None:
        with pytest.raises(SystemExit) as failure:
            worker_count(args)
        assert failure.value.code == pytest.ExitCode.INTERRUPTED
    else:
        chosen = worker_count(args)
        assert chosen == workers
        monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", str(chosen))
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *args, "-n", "auto"], capture_output=True, text=True, timeout=90
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        started = re.search(r"created: (\d+)/", output)
        assert started is not None, output
        assert int(started[1]) == workers, output
        assert f"{selected} passed" in output


@pytest.mark.parametrize("name", ["snapshot.js", "new_contract.js"])
def test_preflight_selects_every_python_wrapper_of_a_changed_javascript_file(tmp_path, monkeypatch, name):
    from pathlib import Path

    import yaml

    from scripts import ci_select, ci_touched_tests
    from scripts.ci_mutation import browser

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/mutation-preflight.yml").read_text())
    command = next(
        step["run"]
        for step in workflow["jobs"]["touched-tests"]["steps"]
        if step.get("name") == "Select the tests the branch touches"
    )
    source = f"scripts/static/{name}"
    files = {
        source: "export const value = 2;\n",
        "tests/test_direct.py": f"PAGE = '{source}'\n",
        "tests/nested/test_basename.py": f"PAGE = '{name}'\n",
        "tests/js_loader.py": f"PAGE = '{name}'\n",
        "tests/second_loader.py": "from tests import js_loader\n",
        "tests/test_indirect.py": "from tests import second_loader\n",
        "tests/test_changed.py": "def test_changed(): pass\n",
        "tests/test_unrelated.py": "def test_other(): pass\n",
    }
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_select, "changed_files", lambda *_: [source, "tests/test_changed.py"])
    collected = []
    monkeypatch.setattr(ci_touched_tests, "worker_count", lambda tests: collected.extend(tests) or 4)
    monkeypatch.setattr(browser, "needs_browser", lambda root, tests: True)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = command.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    exec(compile(code, "<preflight selection>", "exec"), {})
    expected = {
        "tests/test_direct.py",
        "tests/nested/test_basename.py",
        "tests/test_indirect.py",
        "tests/test_changed.py",
    }
    assert set(collected) == expected
    results = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(results["files"].split()) == expected
    assert results["browser"] == "true"
    assert results["workers"] == "4"


@pytest.mark.wall_clock
@pytest.mark.parametrize("job", ["unit", "unit-selected"])
def test_a_hung_worker_fails_before_the_step_timeout(tmp_path, monkeypatch, job):
    import shlex
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    step_name = "Run tests" if job == "unit" else "Run the selected tests"
    command = next(step["run"] for step in workflow["jobs"][job]["steps"] if step.get("name") == step_name)
    options = shlex.split(command)
    assert "--timeout=60" in options
    assert "--timeout-method=thread" in options
    assert "faulthandler_timeout=45" in options
    suite = tmp_path / "test_hung.py"
    suite.write_text("import threading\ndef test_hung():\n    threading.Event().wait()\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-n",
            "1",
            "--max-worker-restart=0",
            "--timeout=1",
            "--timeout-method=thread",
            "-o",
            "faulthandler_timeout=0.5",
            "--tb=short",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_hung" in output
    assert "Timeout" in output
    assert "threading.py" in output
    assert "crashed" in output
