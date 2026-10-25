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
        shipped = {name for name in wheel.namelist() if "/.claude/" in name}

    assert expected
    assert sorted(expected - shipped) == []


def test_wheel_staging_skips_unrelated_trees_and_keeps_package_payload(tmp_path, monkeypatch):
    import tests.test_clean_install_version as clean_install

    source = tmp_path / "package"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname = "agentihooks"\nversion = "0.0.0"\n')
    (source / "README.md").write_text("wheel metadata")
    (source / "LICENSE").write_text("license metadata")
    for name in ("hooks", "scripts", "profiles", "tests", "docs", "evidence"):
        (source / name).mkdir()
        (source / name / "payload").write_text(name)
    hidden = source / "profiles" / "package" / ".claude" / "rules"
    hidden.mkdir(parents=True)
    (hidden / "packaging.md").write_text("hidden payload")
    nested = source / "profiles" / "docs"
    nested.mkdir()
    (nested / "package.md").write_text("nested payload")
    monkeypatch.setattr(clean_install, "REPO", source)

    def build(cmd, **kwargs):
        destination = tmp_path / "build" / "dist"
        destination.mkdir()
        (destination / "agentihooks-0.0.0-py3-none-any.whl").touch()

    monkeypatch.setattr(clean_install, "_run", build)
    clean_install._tagged_wheel(tmp_path / "build")
    staged = tmp_path / "build" / "src"

    for name in ("tests", "docs", "evidence"):
        assert not (staged / name).exists()
    for name in ("hooks", "scripts", "profiles"):
        assert (staged / name / "payload").read_text() == name
    assert (staged / "README.md").read_text() == "wheel metadata"
    assert (staged / "LICENSE").read_text() == "license metadata"
    assert (staged / "pyproject.toml").is_file()
    assert (staged / "profiles/package/.claude/rules/packaging.md").read_text() == "hidden payload"
    assert (staged / "profiles/docs/package.md").read_text() == "nested payload"
