import zipfile

from tests.test_clean_install_version import REPO, _tagged_wheel


def _packaged_dot_claude_files():
    return {
        path.relative_to(REPO).as_posix()
        for path in (REPO / "profiles").rglob("*")
        if path.is_file() and ".claude" in path.parts and "__pycache__" not in path.parts
    }


def test_built_wheel_ships_every_packaged_dot_claude_file(tmp_path):
    expected = _packaged_dot_claude_files()
    with zipfile.ZipFile(_tagged_wheel(tmp_path)) as wheel:
        names = wheel.namelist()
        shipped = {name for name in names if "/.claude/" in name}

    assert expected
    assert sorted(expected - shipped) == []
    assert not [name for name in names if name.startswith(("hooks/", "scripts/"))]
