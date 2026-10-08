import json
from pathlib import Path

from hooks import config
from hooks.context import profile_chain
from scripts.profiles import manifestos, sources
from scripts.targets._common import build_persona, read_manifestos


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


def _scoped(tmp_path: Path) -> Path:
    root = tmp_path / "scoped"
    manifests = root / "manifestos"
    manifests.mkdir(parents=True)
    (manifests / "README.md").write_text("directory docs")
    (manifests / "dev.md").write_text("---\nroles: [engineer, planner]\n---\n# Dev\ndev body\n")
    (manifests / "guard.md").write_text("---\nroles: [cicd]\n---\n\n# Guard\nguard body\n")
    (manifests / "core.md").write_text("# Core\ncore body\n")
    (manifests / "solo.md").write_text("---\nroles: Master\n---\n# Solo\n")
    return root


def _names(paths) -> list[str]:
    return [Path(path).name for path in paths]


def _clean_env(monkeypatch):
    for key in ("CI_MANIFESTO_PATH", "MANIFESTOS_DIR", "AGENTIHOOKS_SKIP_MANIFESTO"):
        monkeypatch.delenv(key, raising=False)


def _profile(tmp_path: Path, name: str, yml: str = "", env: dict | None = None) -> tuple[str, Path]:
    path = tmp_path / "profiles" / name
    (path / ".claude").mkdir(parents=True)
    if yml:
        (path / "profile.yml").write_text(yml)
    if env is not None:
        (path / ".claude" / "settings.overrides.json").write_text(json.dumps({"env": env}))
    return name, path


def _role(name: str) -> tuple[str, Path]:
    return f"package:{name}", profile_chain.PACKAGE_ROLES / name


def test_roles_front_matter_filters_by_role_and_absent_roles_reach_every_role(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)

    assert _names(config._resolve_manifesto_paths(root, role="engineer")) == ["core.md", "dev.md"]
    assert _names(config._resolve_manifesto_paths(root, role="cicd")) == ["core.md", "guard.md"]
    assert _names(config._resolve_manifesto_paths(root, role="master")) == ["core.md", "solo.md"]
    assert _names(config._resolve_manifesto_paths(root, role="QA")) == ["core.md"]
    assert _names(config._resolve_manifesto_paths(root)) == ["core.md", "dev.md", "guard.md", "solo.md"]


def test_choice_includes_and_excludes_by_name_over_the_role(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)

    paths = config._resolve_manifesto_paths(root, role="engineer", choice={"guard": True, "core": False})

    assert _names(paths) == ["dev.md", "guard.md"]


def test_skip_manifesto_still_wins_over_an_include(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("AGENTIHOOKS_SKIP_MANIFESTO", "guard")

    assert _names(config._resolve_manifesto_paths(root, role="cicd", choice={"guard": True})) == ["core.md"]


def test_manifesto_roles_and_body_read_the_front_matter(tmp_path):
    root = _scoped(tmp_path)
    manifests = root / "manifestos"
    (manifests / "broken.md").write_text("---\nroles: [a\n---\n# Broken\n")
    (manifests / "unclosed.md").write_text("---\nroles: [cicd]\n# Unclosed\n")
    (manifests / "listed.md").write_text("---\n- roles\n---\n# Listed\n")

    assert config.manifesto_roles(manifests / "dev.md") == ["engineer", "planner"]
    assert config.manifesto_roles(manifests / "solo.md") == ["master"]
    assert config.manifesto_roles(manifests / "core.md") is None
    assert config.manifesto_roles(manifests / "broken.md") is None
    assert config.manifesto_roles(manifests / "unclosed.md") is None
    assert config.manifesto_roles(manifests / "listed.md") is None
    assert config.manifesto_body(manifests / "dev.md") == "# Dev\ndev body\n"
    assert config.manifesto_body(manifests / "guard.md") == "# Guard\nguard body\n"
    assert config.manifesto_body(manifests / "core.md") == "# Core\ncore body\n"
    assert config.manifesto_body(manifests / "broken.md") == "---\nroles: [a\n---\n# Broken\n"
    assert config.manifesto_body(manifests / "listed.md") == "---\n- roles\n---\n# Listed\n"
    assert config.manifesto_name(" Dev.MD ") == "dev"


def test_chain_paths_take_the_package_role_and_profile_choices_in_chain_order(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)
    base = _profile(tmp_path, "base", "manifestos:\n  include: [Guard.md]\n  exclude: [solo]\n")
    top = _profile(tmp_path, "top", "manifestos:\n  exclude: [guard]\n  include: [solo, core]\n")

    assert _names(manifestos.paths(root, [_role("engineer")])) == ["core.md", "dev.md"]
    assert _names(manifestos.paths(root, [_role("cicd")])) == ["core.md", "guard.md"]
    assert _names(manifestos.paths(root, [base, _role("engineer")])) == ["core.md", "dev.md", "guard.md"]
    assert _names(manifestos.paths(root, [base, top, _role("engineer")])) == ["core.md", "dev.md", "solo.md"]
    assert _names(manifestos.paths(root, [base])) == ["core.md", "dev.md", "guard.md"]
    assert manifestos.choice([base, top]) == {"guard": False, "solo": True, "core": True}


def test_chain_paths_honour_the_enabled_setting_from_the_chain(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("CI_MANIFESTO_ENABLED", "false")
    off = _profile(tmp_path, "off", env={"CI_MANIFESTO_ENABLED": "false"})
    on = _profile(tmp_path, "on", env={"CI_MANIFESTO_ENABLED": "Yes"})
    silent = _profile(tmp_path, "silent", env={"OTHER": "1"})

    assert _names(manifestos.paths(root, [_role("cicd")])) == ["core.md", "guard.md"]
    assert manifestos.paths(root, [off, _role("cicd")]) == []
    assert manifestos.paths(root, [off, silent]) == []
    assert _names(manifestos.paths(root, [off, on])) == ["core.md", "dev.md", "guard.md", "solo.md"]
    assert manifestos.enabled(root, [on, off]) is False
    assert manifestos.enabled(None, []) is True


def test_bundle_settings_layer_can_disable_manifestos(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.overrides.json").write_text(json.dumps({"env": {"CI_MANIFESTO_ENABLED": "0"}}))

    assert manifestos.paths(root, []) == []


def test_persona_holds_only_the_role_manifestos_without_front_matter(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)
    off = _profile(tmp_path, "off", env={"CI_MANIFESTO_ENABLED": "false"})

    engineer = build_persona([_role("engineer")], ["package:engineer"], root, [], "<!-- head -->", "<!-- foot -->")
    cicd = build_persona([_role("cicd")], ["package:cicd"], root, [], "<!-- head -->", "<!-- foot -->")
    silent = build_persona([off, _role("cicd")], ["off"], root, [], "<!-- head -->", "<!-- foot -->")

    assert "<!-- manifesto: dev.md -->\n# Dev\ndev body" in engineer
    assert "# Core" in engineer and "# Guard" not in engineer and "# Solo" not in engineer
    assert "<!-- manifesto: guard.md -->\n# Guard\nguard body" in cicd
    assert "# Core" in cicd and "# Dev" not in cicd
    assert "roles:" not in engineer + cicd
    assert "<!-- ci-manifesto -->" not in silent and "# Core" not in silent


def test_doctrine_sources_list_only_the_role_manifestos(monkeypatch, tmp_path):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)

    files = sources.doctrine_files(root, [_role("cicd")])

    assert [file.name for file in files if file.parent.name == "manifestos"] == ["core.md", "guard.md"]


def test_list_prints_the_role_by_manifesto_matrix(monkeypatch, tmp_path, capsys):
    root = _scoped(tmp_path)
    _clean_env(monkeypatch)

    assert manifestos.main(["list", "--bundle", str(root)]) == 0

    assert capsys.readouterr().out == (
        "manifesto  cicd  engineer  master  planner  qa\n"
        "core.md    x     x         x       x        x\n"
        "dev.md     -     x         -       x        -\n"
        "guard.md   x     -         -       -        -\n"
        "solo.md    -     -         x       -        -\n"
    )


def test_list_names_an_empty_manifesto_folder(monkeypatch, tmp_path, capsys):
    _clean_env(monkeypatch)
    (tmp_path / "empty" / "manifestos").mkdir(parents=True)

    assert manifestos.main(["list", "--bundle", str(tmp_path / "empty")]) == 1

    assert capsys.readouterr().err == f"No manifestos in {tmp_path / 'empty' / 'manifestos'}\n"
