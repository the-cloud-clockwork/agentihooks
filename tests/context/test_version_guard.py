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

_HINT = (
    " Switching to a setuptools-scm tag derived version is allowed once [tool.setuptools_scm] exists:"
    ' replace the version line with dynamic = ["version"] and add no version literal.'
)

_STATIC = """[build-system]
requires = ["setuptools>=61", "setuptools-scm>=8", "wheel"]

[project]
name = "x"
version = "2.17.0"
description = "old"
"""

_TAGGED = _STATIC.replace('version = "2.17.0"', 'dynamic = ["version"]') + "\n[tool.setuptools_scm]\n"

_PYTEST = '\n[tool.pytest.ini_options]\nminversion = "8.0"\n'


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
    assert str(refusal.value) == _BLOCKED.format(name) + (_HINT if name == "pyproject.toml" else "")


def test_write_switching_to_tag_derived_version_is_allowed(tmp_path):
    check_version_guard(_rewrite(_manifest(tmp_path), _TAGGED))


def test_edit_switching_to_tag_derived_version_is_allowed(tmp_path):
    target = _manifest(tmp_path, _STATIC + "\n[tool.setuptools_scm]\n")
    check_version_guard(_edit(target, 'version = "2.17.0"', 'dynamic = ["version"]'))


def test_switch_keeping_other_version_literals_is_allowed(tmp_path):
    target = _manifest(tmp_path, _STATIC + _PYTEST)
    check_version_guard(_rewrite(target, _TAGGED + _PYTEST))


def test_unrelated_manifest_edit_is_allowed(tmp_path):
    check_version_guard(_edit(_manifest(tmp_path), 'description = "old"', 'description = "new"'))


def test_edit_leaving_invalid_toml_without_version_text_is_allowed(tmp_path):
    check_version_guard(_edit(_manifest(tmp_path), 'description = "old"', "description = [broken"))


@pytest.mark.parametrize("replace_all", [True, False])
def test_replace_all_decides_which_occurrences_change(tmp_path, replace_all):
    target = _manifest(tmp_path, '# version = "2.17.0"\n' + _STATIC)
    payload = _edit(target, 'version = "2.17.0"', 'dynamic = ["version"]', replace_all=replace_all)
    if replace_all:
        _refused(payload)
    else:
        check_version_guard(payload)


def test_static_version_bump_is_refused(tmp_path):
    _refused(_edit(_manifest(tmp_path), 'version = "2.17.0"', 'version = "2.18.0"'))


def test_bare_number_bump_is_refused(tmp_path):
    _refused(_edit(_manifest(tmp_path), "2.17.0", "2.18.0"))


def test_static_version_bump_by_write_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _STATIC.replace("2.17.0", "2.18.0")))


def test_static_version_back_on_a_dynamic_manifest_is_refused(tmp_path):
    target = _manifest(tmp_path, _TAGGED)
    _refused(_edit(target, 'dynamic = ["version"]', 'version = "3"'))


def test_switch_without_tag_source_is_refused(tmp_path):
    _refused(_edit(_manifest(tmp_path), 'version = "2.17.0"', 'dynamic = ["version"]'))


def test_switch_without_version_in_dynamic_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED.replace('dynamic = ["version"]', 'dynamic = ["readme"]')))


def test_switch_with_dynamic_as_a_string_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED.replace('dynamic = ["version"]', 'dynamic = "version"')))


def test_switch_without_dynamic_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED.replace('dynamic = ["version"]\n', "")))


def test_switch_keeping_the_static_version_is_refused(tmp_path):
    kept = _TAGGED.replace('dynamic = ["version"]', 'version = "2.17.0"\ndynamic = ["version"]')
    _refused(_rewrite(_manifest(tmp_path), kept))


def test_switch_without_any_tool_table_is_refused(tmp_path):
    target = _manifest(tmp_path, '[project]\nname = "x"\nversion = "2.17.0"\n')
    _refused(_edit(target, 'version = "2.17.0"', 'dynamic = ["version"]'))


def test_project_replaced_by_a_value_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), "project = 1\n\n[tool.setuptools_scm]\n"))


def test_switch_pinning_a_fallback_version_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED + 'fallback_version = "9.9.9"\n'))


def test_switch_pinning_a_fallback_at_the_old_version_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED + 'fallback_version = "2.17.0"\n'))


def test_switch_adding_a_poetry_version_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED + '\n[tool.poetry]\nversion = "9.9.9"\n'))


def test_mypy_python_version_change_is_allowed(tmp_path):
    target = _manifest(tmp_path, _STATIC + '\n[tool.mypy]\npython_version = "3.11"\n')
    check_version_guard(_edit(target, 'python_version = "3.11"', 'python_version = "3.12"'))


def test_cargo_dependency_pin_change_is_allowed(tmp_path):
    target = _manifest(
        tmp_path, '[package]\nversion = "1.0.0"\n\n[dependencies]\ntokio = { version = "1.38" }\n', "Cargo.toml"
    )
    check_version_guard(_edit(target, 'version = "1.38"', 'version = "1.40"'))


def test_epoch_bump_in_an_unreadable_manifest_is_refused(tmp_path):
    target = _manifest(tmp_path, '[project\nversion = "1!2.0"\n')
    _refused(_edit(target, "1!2.0", "1!9.9"))


def test_edit_around_a_version_setting_with_unchanged_values_is_allowed(tmp_path):
    target = _manifest(tmp_path, _TAGGED + _PYTEST)
    check_version_guard(_edit(target, 'minversion = "8.0"', 'minversion = "8.0"\naddopts = "-q"'))


def test_unrelated_edit_on_a_dynamic_manifest_is_allowed(tmp_path):
    check_version_guard(_edit(_manifest(tmp_path, _TAGGED), 'description = "old"', 'description = "new"'))


def test_deleting_a_tool_version_setting_without_new_string_is_allowed(tmp_path):
    target = _manifest(tmp_path, _STATIC + _PYTEST)
    check_version_guard(
        {"tool_name": "Edit", "tool_input": {"file_path": str(target), "old_string": 'minversion = "8.0"\n'}}
    )


def test_write_without_content_on_a_manifest_without_package_version_is_allowed(tmp_path):
    check_version_guard({"tool_name": "Write", "tool_input": {"file_path": str(_manifest(tmp_path, _PYTEST))}})


def test_manifest_that_is_not_utf8_is_still_guarded(tmp_path):
    target = tmp_path / "pyproject.toml"
    target.write_bytes(_STATIC.replace("old", "caf\xe9").encode("latin-1"))
    _refused(_edit(target, "2.17.0", "2.18.0"))


def test_capitalised_version_bump_in_an_unreadable_manifest_is_refused(tmp_path):
    target = _manifest(tmp_path, '[project\nVersion = "1.0.0"\n')
    _refused(_edit(target, "1.0.0", "1.1.0"))


def test_edit_whose_old_string_is_absent_changes_no_version(tmp_path):
    check_version_guard(_edit(_manifest(tmp_path), 'version = "1.0.0"', 'dynamic = ["version"]'))


def test_switch_to_invalid_toml_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path), _TAGGED + "[broken\n"))


def test_package_json_version_edit_is_refused(tmp_path):
    target = _manifest(tmp_path, '{"name": "x", "version": "1.0.0"}\n', "package.json")
    _refused(_edit(target, '"version": "1.0.0"', '"version": "1.1.0"'), "package.json")


def test_package_json_bare_number_bump_is_refused(tmp_path):
    target = _manifest(tmp_path, '{"name": "x", "version": "1.0.0"}\n', "package.json")
    _refused(_edit(target, "1.0.0", "1.1.0"), "package.json")


def test_package_json_unrelated_edit_is_allowed(tmp_path):
    target = _manifest(tmp_path, '{"name": "x", "version": "1.0.0"}\n', "package.json")
    check_version_guard(_edit(target, '"name": "x"', '"name": "y"'))


def test_cargo_manifest_bare_number_bump_is_refused(tmp_path):
    target = _manifest(tmp_path, '[package]\nname = "x"\nversion = "1.0.0"\n', "Cargo.toml")
    _refused(_edit(target, "1.0.0", "1.1.0"), "Cargo.toml")


def test_cargo_manifest_switch_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path, _STATIC, "Cargo.toml"), _TAGGED), "Cargo.toml")


def test_package_json_bump_to_invalid_json_is_refused(tmp_path):
    target = _manifest(tmp_path, '{"name": "x", "version": "1.0.0"}\n', "package.json")
    _refused(_edit(target, '"1.0.0"}', '"1.1.0",'), "package.json")


def test_package_json_reformat_with_the_same_version_is_allowed(tmp_path):
    target = _manifest(tmp_path, '{"name": "x", "version": "1.0.0"}\n', "package.json")
    check_version_guard(_edit(target, '"version": "1.0.0"', '"version":"1.0.0"'))


def test_version_file_bump_is_refused(tmp_path):
    _refused(_rewrite(_manifest(tmp_path, "2.17.0\n", "VERSION"), "2.18.0\n"), "VERSION")


def test_version_txt_bump_is_refused(tmp_path):
    _refused(_edit(_manifest(tmp_path, "2.17.0\n", "version.txt"), "2.17.0", "2.18.0"), "version.txt")


def test_version_file_whitespace_change_is_allowed(tmp_path):
    check_version_guard(_rewrite(_manifest(tmp_path, "2.17.0\n", "VERSION"), " 2.17.0"))


def test_existing_manifest_relative_to_cwd_is_guarded(tmp_path):
    (tmp_path / "sub").mkdir()
    _manifest(tmp_path / "sub")
    payload = _edit("sub/pyproject.toml", "2.17.0", "2.18.0")
    payload["cwd"] = str(tmp_path)
    _refused(payload)


def test_existing_manifest_relative_to_working_directory_is_guarded(tmp_path, monkeypatch):
    _manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    _refused(_edit("pyproject.toml", "2.17.0", "2.18.0"))


def test_codex_patch_bump_is_refused(tmp_path):
    patch = '*** Begin Patch\n*** Update File: pyproject.toml\n-version = "2.17.0"\n+version = "2.18.0"\n*** End Patch'
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(_manifest(tmp_path)), "new_string": patch}}
    _refused(payload)


def test_write_without_content_is_refused(tmp_path):
    _refused({"tool_name": "Write", "tool_input": {"file_path": str(_manifest(tmp_path))}})


def test_poetry_version_bump_is_refused(tmp_path):
    target = _manifest(tmp_path, '[tool.poetry]\nname = "x"\nversion = "1.0.0"\n')
    _refused(_edit(target, "1.0.0", "1.1.0"))


def test_cargo_workspace_version_bump_is_refused(tmp_path):
    target = _manifest(tmp_path, '[workspace.package]\nversion = "1.0.0"\n', "Cargo.toml")
    _refused(_edit(target, "1.0.0", "1.1.0"), "Cargo.toml")


def test_prefixed_fallback_version_on_a_dynamic_manifest_is_refused(tmp_path):
    target = _manifest(tmp_path, _TAGGED)
    _refused(_edit(target, "[tool.setuptools_scm]\n", '[tool.setuptools_scm]\nfallback_version = "v2.18.0"\n'))


def test_switch_changing_another_version_key_is_refused(tmp_path):
    target = _manifest(tmp_path, _STATIC + '\n[tool.poetry]\nversion = "1.0"\n')
    _refused(_rewrite(target, _TAGGED + '\n[tool.poetry]\nversion = "1.1"\n'))


def test_unrelated_key_ending_in_version_is_not_a_version_key(tmp_path):
    target = _manifest(tmp_path, _STATIC + '\n[tool.ruff]\ntarget-version = "py311"\n')
    check_version_guard(_edit(target, 'target-version = "py311"', 'target-version = "py312"'))
