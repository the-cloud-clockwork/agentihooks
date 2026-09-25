"""Tests for hooks.context.version_guard."""

import pytest

from hooks.context.version_guard import check_version_guard
from hooks.hook_manager import BlockAction


def _write(path, cwd=""):
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": '[project]\nname = "x"\nversion = "0.1.0"\n'},
        "cwd": cwd,
    }


def test_new_manifest_may_declare_version(tmp_path):
    check_version_guard(_write(tmp_path / "pyproject.toml"))


def test_new_manifest_relative_to_cwd(tmp_path):
    check_version_guard(_write("backend/pyproject.toml", cwd=str(tmp_path)))


def test_existing_manifest_version_change_blocked(tmp_path):
    target = tmp_path / "pyproject.toml"
    target.write_text('[project]\nversion = "0.0.9"\n')
    with pytest.raises(BlockAction):
        check_version_guard(_write(target))
