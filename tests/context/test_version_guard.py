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


_BLOCKED = (
    "BLOCKED: Version field modification in {} is not allowed. "
    "Version bumping is handled by the release workflow (gh workflow run release.yml -f bump=patch|minor|major). "
    "Do not edit version fields manually."
)

_STATIC = """[build-system]
requires = ["setuptools>=61", "setuptools-scm>=8", "wheel"]

[project]
name = "x"
version = "2.17.0"

[tool.pytest.ini_options]
minversion = "8.0"
"""

_TAGGED = _STATIC.replace('version = "2.17.0"', 'dynamic = ["version"]') + "\n[tool.setuptools_scm]\n"


def _manifest(tmp_path, text=_STATIC, name="pyproject.toml"):
    target = tmp_path / name
    target.write_text(text)
    return target


def _edit(target, old, new, replace_all=False):
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target), "old_string": old, "new_string": new, "replace_all": replace_all},
        "cwd": "",
    }


def _rewrite(target, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}, "cwd": ""}


def _refused(payload, name="pyproject.toml"):
    with pytest.raises(BlockAction) as refusal:
        check_version_guard(payload)
    assert str(refusal.value) == _BLOCKED.format(name)


def test_write_switching_to_tag_derived_version_is_allowed(tmp_path):
    check_version_guard(_rewrite(_manifest(tmp_path), _TAGGED))


def test_edit_switching_to_tag_derived_version_is_allowed(tmp_path):
    target = _manifest(tmp_path, _STATIC + "\n[tool.setuptools_scm]\n")
    check_version_guard(_edit(target, 'version = "2.17.0"', 'dynamic = ["version"]'))


def test_edit_with_replace_all_switching_to_tag_derived_version_is_allowed(tmp_path):
    target = _manifest(tmp_path, _STATIC + "\n[tool.setuptools_scm]\n")
    check_version_guard(_edit(target, 'version = "2.17.0"', 'dynamic = ["version"]', replace_all=True))


def test_static_version_bump_is_refused(tmp_path):
    _refused(_edit(_manifest(tmp_path), 'version = "2.17.0"', 'version = "2.18.0"'))


def test_static_version_bump_by_write_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _STATIC.replace("2.17.0", "2.18.0")))


def test_switch_without_tag_source_is_refused(tmp_path):
    _refused(_edit(_manifest(tmp_path), 'version = "2.17.0"', 'dynamic = ["version"]'))


def test_switch_without_version_in_dynamic_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED.replace('dynamic = ["version"]', 'dynamic = ["readme"]')))


def test_switch_pinning_a_fallback_version_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED + 'fallback_version = "9.9.9"\n'))


def test_tag_source_edit_on_a_dynamic_manifest_is_refused(tmp_path):
    target = _manifest(tmp_path, _TAGGED)
    _refused(_edit(target, "[tool.setuptools_scm]\n", '[tool.setuptools_scm]\nfallback_version = "3.0.0"\n'))


def test_switch_with_missing_old_string_is_refused(tmp_path):
    target = _manifest(tmp_path, _STATIC + "\n[tool.setuptools_scm]\n")
    _refused(_edit(target, 'version = "1.0.0"', 'dynamic = ["version"]'))


def test_switch_to_invalid_toml_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED + "[broken\n"))


def test_switch_keeping_the_static_version_is_refused(tmp_path):
    kept = _TAGGED.replace('dynamic = ["version"]', 'version = "2.17.0"\ndynamic = ["version"]')
    _refused(_rewrite(_manifest(tmp_path), kept))


def test_switch_without_dynamic_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED.replace('dynamic = ["version"]\n', "")))


def test_switch_without_any_tool_table_is_refused(tmp_path):
    target = _manifest(tmp_path, '[project]\nname = "x"\nversion = "2.17.0"\n')
    _refused(_edit(target, 'version = "2.17.0"', 'dynamic = ["version"]'))


def test_version_edit_on_an_already_dynamic_manifest_is_refused(tmp_path):
    target = _manifest(tmp_path, _TAGGED)
    _refused(_edit(target, 'minversion = "8.0"', 'minversion = "8.0"\naddopts = "-q"'))


@pytest.mark.parametrize("replace_all", [True, False])
def test_switch_replaces_every_occurrence_only_with_replace_all(tmp_path, replace_all):
    target = _manifest(tmp_path, '# version = "2.17.0"\n' + _STATIC + "\n[tool.setuptools_scm]\n")
    payload = _edit(target, 'version = "2.17.0"', 'dynamic = ["version"]', replace_all=replace_all)
    if replace_all:
        check_version_guard(payload)
    else:
        _refused(payload)


def test_package_json_version_edit_is_refused(tmp_path):
    target = _manifest(tmp_path, '{"name": "x", "version": "1.0.0"}\n', "package.json")
    _refused(_edit(target, '"version": "1.0.0"', '"version": "1.1.0"'), "package.json")


def test_cargo_manifest_version_edit_is_refused(tmp_path):
    target = _manifest(tmp_path, _STATIC, "Cargo.toml")
    _refused(_rewrite(target, _TAGGED), "Cargo.toml")
