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
