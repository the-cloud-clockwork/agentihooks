from scripts.ci_mutation.__main__ import main


def test_cli_uses_requested_revisions_budget_output_and_exit_code(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)

    def discover(root, base, head):
        calls.append((root, base, head))
        return {"hooks/sample.py": {2}}

    def gate(root, changes, output, budget):
        calls.append((root, changes, output, budget))
        return {"failed": True}

    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    monkeypatch.setattr(
        "sys.argv", ["gate", "--base", "base", "--head", "head", "--budget", "13", "--output", "evidence"]
    )
    assert main() == 1
    assert calls == [(tmp_path, "base", "head"), (tmp_path, {"hooks/sample.py": {2}}, tmp_path / "evidence", 13)]
    assert "Changed Python files: 1" in capsys.readouterr().out
    assert __import__("os").environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_cli_defaults_and_success(tmp_path, monkeypatch):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["gate"])

    def discover(root, base, head):
        assert (root, base, head) == (tmp_path, "origin/dev", "HEAD")
        return {}

    def gate(root, changes, output, budget):
        assert (root, changes, output, budget) == (tmp_path, {}, tmp_path / ".mutation-gate", 1080)
        return {"failed": False}

    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    assert main() == 0


def test_browser_preflight_skips_test_only_changes(tmp_path, monkeypatch, capsys):
    from scripts.ci_mutation import browser

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browser, "discover_changes", lambda *args: {})
    monkeypatch.setattr("sys.argv", ["browser", "--base", "base"])
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert browser.main() == 0
    assert output.read_text() == "browser=false\n"
    assert "Selected mutation tests: 0" in capsys.readouterr().out


def test_browser_preflight_uses_mutation_test_selection(tmp_path, monkeypatch, capsys):
    from scripts.ci_mutation import browser

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_page.py").write_text('import pytest\npytest.importorskip("playwright.sync_api")\n')
    monkeypatch.chdir(tmp_path)
    calls = []

    def select(root, source):
        calls.append((root, source))
        return ["tests/test_page.py"]

    monkeypatch.setattr(browser, "discover_changes", lambda *args: {"scripts/page.py": {1}})
    monkeypatch.setattr(browser, "select_tests", select)
    monkeypatch.setattr("sys.argv", ["browser", "--base", "base"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert browser.main() == 0
    assert calls == [(tmp_path, __import__("pathlib").Path("scripts/page.py"))]
    assert "Browser required: true" in capsys.readouterr().out


def test_browser_detection_follows_local_imports_and_parent_fixtures(tmp_path):
    from scripts.ci_mutation.browser import needs_browser

    folder = tmp_path / "tests/page"
    folder.mkdir(parents=True)
    test = folder / "test_page.py"
    test.write_text("from .helper import render\n")
    helper = folder / "helper.py"
    helper.write_text("from tests.page import shared\n")
    (folder / "shared.py").write_text("from playwright.sync_api import sync_playwright\n")
    assert needs_browser(tmp_path, ["tests/page/test_page.py"]) is True
    helper.write_text("")
    (tmp_path / "tests/conftest.py").write_text("import playwright.async_api\n")
    assert needs_browser(tmp_path, ["tests/page/test_page.py"]) is True


def test_browser_detection_skips_plain_tests_and_handles_import_cycles(tmp_path):
    from scripts.ci_mutation.browser import needs_browser

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_plain.py").write_text("from tests import helper\n")
    (tmp_path / "tests/helper.py").write_text("import tests.test_plain\n")
    assert needs_browser(tmp_path, ["tests/test_plain.py"]) is False
