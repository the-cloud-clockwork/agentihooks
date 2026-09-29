from pathlib import Path

from hooks import config
from scripts.targets._common import read_manifestos


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    manifests = root / "manifestos"
    manifests.mkdir(parents=True)
    (manifests / "README.md").write_text("directory docs")
    (manifests / "uno.md").write_text("# Uno")
    (manifests / "dos.md").write_text("# Dos")
    return root


def test_bundle_loads_every_manifesto_in_filename_order(monkeypatch, tmp_path):
    root = _bundle(tmp_path)
    monkeypatch.delenv("CI_MANIFESTO_PATH", raising=False)
    monkeypatch.delenv("MANIFESTOS_DIR", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SKIP_MANIFESTO", raising=False)

    paths = config._resolve_manifesto_paths(root)

    assert [Path(path).name for path in paths] == ["dos.md", "uno.md"]
    content = read_manifestos(root)
    assert "# Dos" in content
    assert "# Uno" in content
    assert "directory docs" not in content


def test_skip_manifesto_is_comma_separated_and_extension_optional(monkeypatch, tmp_path):
    root = _bundle(tmp_path)
    monkeypatch.delenv("CI_MANIFESTO_PATH", raising=False)
    monkeypatch.delenv("MANIFESTOS_DIR", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SKIP_MANIFESTO", "UNO.md, tres")

    paths = config._resolve_manifesto_paths(root)

    assert [Path(path).name for path in paths] == ["dos.md"]


def test_explicit_manifesto_path_remains_single_file_override(monkeypatch, tmp_path):
    root = _bundle(tmp_path)
    explicit = tmp_path / "explicit.md"
    explicit.write_text("# Explicit")
    monkeypatch.setenv("CI_MANIFESTO_PATH", str(explicit))

    assert config._resolve_manifesto_paths(root) == [str(explicit)]
