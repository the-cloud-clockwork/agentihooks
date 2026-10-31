from pathlib import Path

import pytest

from scripts import fixture_patch_lint


MOCK_FIXTURE = """
from unittest.mock import patch as replace
import pytest

@pytest.fixture(autouse=True)
def isolated():
    with replace("hooks.context.enforcement._get_bundle_path", return_value=None):
        yield
"""

OVERRIDE = """
from hooks.context import enforcement as rules

def test_override(monkeypatch):
    monkeypatch.setattr(rules, "_get_bundle_path", lambda: None)
"""


def write(root: Path, name: str, source: str) -> Path:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_planted_mix_turns_the_lint_red_and_monkeypatch_repair_turns_it_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, "test_mix.py", MOCK_FIXTURE + OVERRIDE)
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "tests/test_mix.py" in output
    assert "hooks.context.enforcement._get_bundle_path" in output
    assert "isolated" in output
    assert "test_override" in output
    path.write_text(
        """
import pytest

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr("hooks.context.enforcement._get_bundle_path", lambda: None)
"""
        + OVERRIDE
    )
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 0
    assert "0 overlaps" in capsys.readouterr().out


@pytest.mark.parametrize("scope", ["conftest.py", "nested/conftest.py"])
def test_parent_autouse_mock_fixture_is_checked_against_descendant_overrides(tmp_path: Path, scope: str) -> None:
    write(tmp_path, scope, MOCK_FIXTURE)
    write(tmp_path, "nested/test_mix.py", OVERRIDE)
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 1


def test_sibling_conftest_and_unrelated_attributes_do_not_overlap(tmp_path: Path) -> None:
    write(tmp_path, "other/conftest.py", MOCK_FIXTURE)
    write(tmp_path, "test_clean.py", OVERRIDE)
    write(tmp_path, "test_distinct.py", MOCK_FIXTURE + OVERRIDE.replace('"_get_bundle_path"', '"_store_path"'))
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 0


def test_object_patch_and_dotted_monkeypatch_targets_resolve_import_aliases(tmp_path: Path) -> None:
    write(
        tmp_path,
        "test_aliases.py",
        """
import unittest.mock as mocks
from hooks.context import enforcement as rules
from pytest import fixture

@fixture(autouse=True)
def isolated():
    with mocks.patch.object(rules, "_get_bundle_path", return_value=None):
        yield

def test_override(monkeypatch):
    monkeypatch.setattr("hooks.context.enforcement._get_bundle_path", lambda: None)
""",
    )
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 1


def test_mock_context_inside_a_test_has_its_own_teardown(tmp_path: Path) -> None:
    write(
        tmp_path,
        "test_scoped.py",
        """
from unittest.mock import patch

def test_override(monkeypatch):
    monkeypatch.setattr("hooks.context.enforcement._get_bundle_path", lambda: None)
    with patch("hooks.context.enforcement._get_bundle_path", return_value=None):
        pass
""",
    )
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 0


def test_empty_test_tree_is_not_a_passing_lint(tmp_path: Path) -> None:
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 1


def test_invalid_source_fails_with_its_location(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path, "test_invalid.py", "def broken(")
    assert fixture_patch_lint.main(["--root", str(tmp_path)]) == 1
    assert "tests/test_invalid.py: cannot parse" in capsys.readouterr().out
