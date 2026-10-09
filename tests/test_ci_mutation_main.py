import ast
from pathlib import Path

import pytest

from scripts.ci_mutation.__main__ import main


def test_cli_uses_requested_revisions_budget_output_and_exit_code(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)

    def discover(root, base, head):
        calls.append((root, base, head))
        return {"hooks/sample.py": {2}}

    def gate(root, changes, output, budget, shard, stats):
        calls.append((root, changes, output, budget, shard, stats))
        return {"failed": True}

    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    monkeypatch.setattr(
        "sys.argv",
        [
            "gate",
            "--base",
            "base",
            "--head",
            "head",
            "--budget",
            "13",
            "--output",
            "evidence",
            "--shard",
            "2",
            "--shards",
            "5",
        ],
    )
    assert main() == 1
    assert calls == [
        (tmp_path, "base", "head"),
        (tmp_path, {"hooks/sample.py": {2}}, tmp_path / "evidence", 13, (2, 5), None),
    ]
    out = capsys.readouterr().out
    assert "Changed Python files: 1" in out
    assert "Mutation shard: 3 of 5" in out
    assert __import__("os").environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_cli_defaults_and_success(tmp_path, monkeypatch):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["gate"])

    def discover(root, base, head):
        assert (root, base, head) == (tmp_path, "origin/dev", "HEAD")
        return {}

    def gate(root, changes, output, budget, shard, stats):
        assert (root, changes, output, budget, shard, stats) == (
            tmp_path,
            {},
            tmp_path / ".mutation-gate",
            1080,
            (0, 1),
            None,
        )
        return {"failed": False}

    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    assert main() == 0


@pytest.mark.parametrize(
    ("extra", "part", "line"),
    [
        ([], None, "Mutation stats from shared"),
        (["--stats-part", "1", "--stats-parts", "3"], (1, 3), "Mutation stats part: 2 of 3"),
    ],
)
def test_cli_passes_shared_stats_keyed_to_the_resolved_head(tmp_path, monkeypatch, capsys, extra, part, line):
    from scripts.ci_mutation.stats import SharedStats

    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["gate", "--head", "topic", "--stats", "shared", *extra])
    runs = []

    def run(command, **kwargs):
        runs.append((command, kwargs))
        return __import__("subprocess").CompletedProcess(command, 0, "abc123\n", "")

    def gate(root, changes, output, budget, shard, stats):
        assert stats == SharedStats(tmp_path / "shared", "abc123", part)
        return {"failed": False}

    monkeypatch.setattr("scripts.ci_mutation.__main__.subprocess.run", run)
    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", lambda root, base, head: {})
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    assert main() == 0
    assert runs == [
        (["git", "rev-parse", "topic"], {"cwd": tmp_path, "capture_output": True, "text": True, "check": True})
    ]
    assert line in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["--stats-part", "0"],
        ["--stats", "s", "--stats-part", "2", "--stats-parts", "2"],
        ["--stats", "s", "--stats-part", "-1"],
    ],
)
def test_cli_refuses_a_stats_part_outside_its_matrix_or_without_a_folder(tmp_path, monkeypatch, capsys, args):
    monkeypatch.setattr("sys.argv", ["gate", *args])
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", lambda *args: pytest.fail("ran without a valid part"))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "needs --stats and is outside 0 to" in capsys.readouterr().err


@pytest.mark.parametrize(("shard", "shards"), [("3", "3"), ("-1", "2"), ("0", "0")])
def test_cli_refuses_a_shard_outside_its_matrix(tmp_path, monkeypatch, capsys, shard, shards):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["gate", "--shard", shard, "--shards", shards])
    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", lambda *args: pytest.fail("graded"))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert capsys.readouterr().err.endswith(f"error: --shard {shard} is outside 0 to {int(shards) - 1}\n")


def test_browser_preflight_skips_test_only_changes(tmp_path, monkeypatch, capsys):
    from scripts.ci_mutation import browser

    monkeypatch.chdir(tmp_path)

    def discover(root, base, head):
        assert (root, base, head) == (tmp_path, "base", "HEAD")
        return {}

    monkeypatch.setattr(browser, "discover_changes", discover)
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
    assert calls == [(tmp_path, Path("scripts/page.py"))]
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


def test_browser_detection_does_not_follow_unrelated_application_imports(tmp_path):
    from scripts.ci_mutation.browser import needs_browser

    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests/test_plain.py").write_text("from scripts import app\n")
    (tmp_path / "tests/conftest.py").write_text("from scripts import app\n")
    (tmp_path / "scripts/app.py").write_text("def render():\n    import playwright.sync_api\n")
    assert needs_browser(tmp_path, ["tests/test_plain.py"]) is False


def test_browser_preflight_defaults_and_appends_output(tmp_path, monkeypatch, capsys):
    from scripts.ci_mutation import browser

    def discover(root, base, head):
        assert (root, base, head) == (tmp_path, "origin/dev", "HEAD")
        return {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browser, "discover_changes", discover)
    monkeypatch.setattr("sys.argv", ["browser"])
    output = tmp_path / "outputs"
    output.write_text("earlier=output\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert browser.main() == 0
    assert output.read_text() == "earlier=output\nbrowser=false\n"
    assert capsys.readouterr().out == "Selected mutation tests: 0\nBrowser required: false\n"


def test_browser_local_imports_resolve_parent_package_and_uppercase_module(tmp_path):
    from scripts.ci_mutation.browser import imported_paths

    folder = tmp_path / "tests/sub"
    folder.mkdir(parents=True)
    (tmp_path / "tests/helper.py").touch()
    (folder / "__init__.py").touch()
    (tmp_path / "tests/X.py").touch()
    test = folder / "test_page.py"
    nodes = list(ast.walk(ast.parse("from .. import helper\nfrom tests import sub\nfrom tests.X import render\n")))
    assert imported_paths(tmp_path, test, nodes) == [
        tmp_path / "tests/helper.py",
        folder / "__init__.py",
        tmp_path / "tests/X.py",
    ]


def test_browser_detection_checks_package_initializers_and_dynamic_calls(tmp_path):
    from scripts.ci_mutation.browser import needs_browser

    folder = tmp_path / "tests/package"
    folder.mkdir(parents=True)
    test = folder / "test_page.py"
    test.write_text("")
    initializer = folder / "__init__.py"
    initializer.write_text("import playwright.sync_api\n")
    assert needs_browser(tmp_path, ["tests/package/test_page.py"]) is True
    initializer.write_text("")
    test.write_text('print("ordinary string")\n')
    assert needs_browser(tmp_path, ["tests/package/test_page.py"]) is False
    test.write_text('import importlib\nimportlib.import_module("playwright")\n')
    assert needs_browser(tmp_path, ["tests/package/test_page.py"]) is True


def test_browser_cycles_do_not_hide_later_tests_or_revisit_modules(tmp_path, monkeypatch):
    from scripts.ci_mutation import browser

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_page.py").write_text("import playwright.sync_api\n")
    (tmp_path / "tests/test_cycle.py").write_text("import tests.helper\n")
    (tmp_path / "tests/helper.py").write_text("import tests.test_cycle\n")
    original = browser.imported_paths
    seen = set()

    def imports(root, path, nodes):
        assert path not in seen
        seen.add(path)
        return original(root, path, nodes)

    monkeypatch.setattr(browser, "imported_paths", imports)
    assert browser.needs_browser(tmp_path, ["tests/test_page.py", "tests/test_cycle.py"]) is True
