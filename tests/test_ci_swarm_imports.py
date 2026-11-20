import importlib
from pathlib import Path

import pytest

ci_swarm_imports = importlib.import_module("scripts.ci_swarm_imports")
pytestmark = pytest.mark.unit


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
    assert ci_swarm_imports.main(["--root", str(root)]) == 1
    assert capsys.readouterr().out == (
        "PINNED packaging: hooks/probe.py:1\n"
        "UNPINNED unpinned_dependency: hooks/probe.py:2\n"
        "2 third party imports; 1 unpinned\n"
    )


def test_pinned_imports_include_aliases_and_dotted_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = source_tree(
        tmp_path,
        "import jwt, yaml\nfrom packaging.version import Version\nimport tree_sitter_bash\nfrom dotenv import load_dotenv\nfrom opentelemetry import trace\n",
        "PyJWT==2.15.1\nPyYAML==6.0.3\npackaging==26.3 \\\n    --hash=sha256:abc\ntree-sitter-bash==0.25.1\npython-dotenv==1.2.4\nopentelemetry-api==1.42.0\n",
    )
    assert ci_swarm_imports.main(["--root", str(root)]) == 0
    output = capsys.readouterr().out
    for module in ("jwt", "yaml", "packaging", "tree_sitter_bash", "dotenv", "opentelemetry"):
        assert module in output
    assert "UNPINNED" not in output


def test_stdlib_relative_local_and_deferred_imports_are_excluded(tmp_path: Path) -> None:
    root = source_tree(
        tmp_path,
        "import os\nfrom pathlib import Path\nfrom . import sibling\nimport scripts\nimport sibling\nimport ancestor\ndef deferred():\n    import deferred_dependency\n",
        "",
    )
    (root / "hooks" / "sibling.py").write_text("")
    (root / "ancestor.py").write_text("")
    assert ci_swarm_imports.check(root) == ({}, set())


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


def test_current_repository_imports_are_all_pinned() -> None:
    root = Path(__file__).resolve().parents[1]
    imported, missing = ci_swarm_imports.check(root)
    assert "packaging" in imported
    assert missing == set(), missing


@pytest.mark.parametrize(
    "module",
    [
        "opentelemetry.sdk.metrics",
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    ],
)
def test_namespace_import_requires_its_own_distribution(tmp_path: Path, module: str) -> None:
    root = source_tree(tmp_path, f"import {module}\n", "opentelemetry-api==1.45.1\n")
    assert ci_swarm_imports.check(root)[1] == {"opentelemetry"}


def test_namespace_imports_accept_their_own_pins(tmp_path: Path) -> None:
    root = source_tree(
        tmp_path,
        "from opentelemetry import sdk\nfrom opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter\n",
        "opentelemetry-sdk==1.45.1\nopentelemetry-exporter-otlp-proto-http==1.45.1\n",
    )
    assert ci_swarm_imports.check(root)[1] == set()
