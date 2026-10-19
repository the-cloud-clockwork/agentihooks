import re

import pytest


@pytest.mark.parametrize(("selected", "maximum", "workers"), [(4, 8, 4), (10, 2, 2), (1, 8, 1), (None, 8, None)])
def test_touched_test_workers_follow_the_collected_selection(tmp_path, monkeypatch, capfd, selected, maximum, workers):
    from scripts.ci_touched_tests import main

    suite = tmp_path / "test_selection.py"
    suite.write_text(
        "invalid python!\n"
        if selected is None
        else f"import pytest\n@pytest.mark.parametrize('case', range({selected}))\ndef test_case(case):\n    pass\n"
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", str(maximum))
    status = main([str(suite), "--tb=short", "-p", "no:cacheprovider"])
    captured = capfd.readouterr()
    output = captured.out + captured.err
    if selected is None:
        assert status != 0, output
        assert "SyntaxError" in output
        assert "created:" not in output
    else:
        assert status == 0, output
        started = re.search(r"created: (\d+)/", output)
        assert started is not None, output
        assert int(started[1]) == workers, output
        assert f"{selected} passed" in output
