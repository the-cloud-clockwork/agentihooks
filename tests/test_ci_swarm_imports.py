from pathlib import Path

import pytest
import yaml

from scripts import ci_swarm_imports

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def source_tree(tmp_path: Path, source: str, lock: str = "packaging==26.3\n") -> Path:
    root = tmp_path / "source"
    for package in ("scripts", "hooks"):
        folder = root / package
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text("")
    (root / "hooks" / "probe.py").write_text(source)
    folder = root / "docker" / "swarm"
    folder.mkdir(parents=True)
    (folder / "requirements.lock").write_text(lock)
    return root


@pytest.mark.feature("swarm-image-import-pins")
def test_unpinned_module_import_turns_the_check_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = source_tree(tmp_path, "import packaging\nimport unpinned_dependency\n")
    (root / "scripts" / "probe.py").write_text("from unpinned_dependency.module import value\n")
    assert ci_swarm_imports.main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "PINNED packaging (packaging): hooks/probe.py:1",
        "UNPINNED unpinned_dependency (unpinned-dependency): hooks/probe.py:2, scripts/probe.py:1",
        "2 third party imports; 1 unpinned",
    ]


@pytest.mark.feature("swarm-image-import-pins")
def test_pinned_imports_include_aliases_and_dotted_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = source_tree(
        tmp_path,
        "import jwt, yaml.loader\nfrom packaging.version import Version\nimport tree_sitter_bash\nfrom dotenv import load_dotenv\nfrom opentelemetry import trace\n",
        "PyJWT==2.15.1\nPyYAML==6.0.3\npackaging==26.3 \\\n    --hash=sha256:abc\ntree_sitter_bash==0.25.1\npython-dotenv==1.2.4\nopentelemetry-api==1.42.0\n",
    )
    monkeypatch.chdir(root)
    assert ci_swarm_imports.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "PINNED dotenv (python-dotenv): hooks/probe.py:4",
        "PINNED jwt (pyjwt): hooks/probe.py:1",
        "PINNED opentelemetry (opentelemetry-api): hooks/probe.py:5",
        "PINNED packaging (packaging): hooks/probe.py:2",
        "PINNED tree_sitter_bash (tree-sitter-bash): hooks/probe.py:3",
        "PINNED yaml (pyyaml): hooks/probe.py:1",
        "6 third party imports; 0 unpinned",
    ]


def test_stdlib_relative_local_and_deferred_imports_are_excluded(tmp_path: Path) -> None:
    root = source_tree(
        tmp_path,
        "import os\nfrom pathlib import Path\nfrom . import sibling\nfrom .relative_only import value\nimport scripts\nimport ancestor\ndef deferred():\n    import deferred_dependency\n",
        "",
    )
    (root / "ancestor.py").write_text("")
    assert ci_swarm_imports.check(root) == ({}, set())


def test_a_module_beside_or_above_the_importer_in_a_nested_folder_is_local(tmp_path: Path) -> None:
    root = source_tree(tmp_path, "")
    ledger = root / "scripts" / "ledger"
    (ledger / "repository").mkdir(parents=True)
    (ledger / "repository" / "mutation.py").write_text("import ledger_core\nimport ledger_bin\nimport neighbour\n")
    (ledger / "repository" / "neighbour.py").write_text("")
    (ledger / "ledger_core.py").write_text("")
    (ledger / "ledger_bin").mkdir()
    (ledger / "ledger_bin" / "__init__.py").write_text("")
    assert ci_swarm_imports.check(root) == ({}, set())


def test_a_top_level_package_folder_or_one_above_the_root_does_not_hide_a_dependency(tmp_path: Path) -> None:
    root = source_tree(tmp_path, "import sibling\n", "")
    (root / "hooks" / "sibling.py").write_text("")
    (root / "hooks" / "mcp").mkdir()
    (root / "hooks" / "mcp" / "__init__.py").write_text("")
    (root / "hooks" / "router").mkdir()
    (root / "hooks" / "router" / "server.py").write_text("from mcp.types import Tool\nimport packaging\n")
    (tmp_path / "packaging.py").write_text("")
    assert ci_swarm_imports.check(root) == (
        {
            "mcp": ["hooks/router/server.py:1"],
            "packaging": ["hooks/router/server.py:2"],
            "sibling": ["hooks/probe.py:1"],
        },
        {"mcp", "packaging", "sibling"},
    )


def test_an_unrelated_repository_directory_does_not_hide_a_dependency(tmp_path: Path) -> None:
    root = source_tree(tmp_path, "import packaging\n", "")
    (root / "scripts" / "packaging").mkdir()
    imported, missing = ci_swarm_imports.check(root)
    assert imported == {"packaging": ["hooks/probe.py:1"]}
    assert missing == {"packaging"}


@pytest.mark.parametrize("requirement", ["packaging>=24", "packaging==26.*", "other==26.3", ""])
def test_only_exact_distribution_pins_satisfy_an_import(tmp_path: Path, requirement: str) -> None:
    root = source_tree(tmp_path, "import packaging\n", requirement)
    assert ci_swarm_imports.check(root)[1] == {"packaging"}


def test_every_nested_source_file_is_walked(tmp_path: Path) -> None:
    root = source_tree(tmp_path, "")
    folder = root / "scripts" / "nested"
    folder.mkdir()
    (folder / "probe.py").write_text("from nested_dependency.module import value\n")
    assert ci_swarm_imports.check(root) == (
        {"nested_dependency": ["scripts/nested/probe.py:1"]},
        {"nested_dependency"},
    )


def test_missing_source_or_lock_fails_closed(tmp_path: Path) -> None:
    root = source_tree(tmp_path, "")
    (root / "docker" / "swarm" / "requirements.lock").unlink()
    with pytest.raises(FileNotFoundError):
        ci_swarm_imports.check(root)
    with pytest.raises(ValueError, match="hooks"):
        ci_swarm_imports.check(root / "missing")


def test_a_source_that_does_not_parse_names_its_file(tmp_path: Path) -> None:
    root = source_tree(tmp_path, "import (\n")
    with pytest.raises(SyntaxError) as raised:
        ci_swarm_imports.check(root)
    assert raised.value.filename == str(root / "hooks" / "probe.py")


@pytest.mark.feature("swarm-image-import-pins")
def test_the_required_lint_job_runs_the_check_on_pull_requests() -> None:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())["jobs"]
    runs = [step.get("run", "") for step in jobs["lint"]["steps"]]
    assert any('python -m scripts.ci_swarm_imports --root "$GITHUB_WORKSPACE"' in run for run in runs)
    assert "lint" in jobs["gate-required"]["needs"]
