import importlib
import json

import pytest
import yaml

from hooks.context import profile_chain
from scripts import install


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    bundle = tmp_path / "bundle"
    roles = tmp_path / "roles"
    for root in (builtin, bundle / "profiles", roles):
        root.mkdir(parents=True)
    linked = []
    monkeypatch.setattr(install, "PROFILES_DIR", builtin)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: bundle)
    monkeypatch.setattr(install, "_get_linked_profiles", lambda: linked)
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)

    def add(source, name, description="", capabilities=()):
        root = {"builtin": builtin, "bundle": bundle / "profiles", "package": roles}.get(source)
        if root is None:
            root = tmp_path / "linked"
            linked.append({"name": name, "path": str(root / name)})
        path = root / name
        path.mkdir(parents=True)
        (path / "profile.yml").write_text(
            yaml.safe_dump(
                {
                    "description": description,
                    "capabilities": list(capabilities),
                }
            )
        )
        return path

    return add


def cli():
    return importlib.import_module("scripts.agents.cli")


def test_catalogue_covers_sources_and_lookup_precedence(catalogue):
    catalogue("builtin", "same", "builtin", ["code", "code"])
    catalogue("bundle", "same", "bundle")
    catalogue("linked", "same", "linked")
    catalogue("package", "same", "package")
    catalogue("bundle", "bundle", "bundle", ["infra"])
    catalogue("linked", "bundle", "shadowed")
    catalogue("package", "bundle", "shadowed")
    catalogue("linked", "linked", "linked")
    catalogue("package", "linked", "shadowed")
    catalogue("package", "role", "role")
    catalogue("builtin", "_hidden")
    assert cli().list_cards() == [
        {"name": "bundle", "description": "bundle", "capabilities": ["infra"]},
        {"name": "linked", "description": "linked", "capabilities": []},
        {"name": "role", "description": "role", "capabilities": []},
        {"name": "same", "description": "builtin", "capabilities": ["code"]},
    ]


def test_list_json_and_capability_filter(catalogue, capsys):
    catalogue("bundle", "coder", "Writes code", ["code", "review"])
    catalogue("package", "reviewer", "Reviews", ["review"])
    assert cli().main(["list", "--capability", "code"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": "coder", "description": "Writes code", "capabilities": ["code", "review"]},
    ]
    assert cli().list_cards("missing") == []
    assert len(cli().list_cards("review")) == 2
    assert cli().main(["list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_show_json_and_explicit_package_role(catalogue, capsys):
    catalogue("bundle", "engineer", "Custom")
    catalogue("package", "engineer", "Base", ["code"])
    assert cli().main(["show", "engineer"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "name": "engineer",
        "description": "Custom",
        "capabilities": [],
    }
    assert cli().show_card("package:engineer") == {
        "name": "package:engineer",
        "description": "Base",
        "capabilities": ["code"],
    }


def test_output_is_indented_json(catalogue, capsys):
    catalogue("bundle", "engineer", "Custom", ["code"])
    assert cli().main(["show", "engineer"]) == 0
    assert capsys.readouterr().out == (
        '{\n  "name": "engineer",\n  "description": "Custom",\n  "capabilities": [\n    "code"\n  ]\n}\n'
    )


def _help(capsys, args):
    with pytest.raises(SystemExit) as exc:
        cli().main(args)
    assert exc.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def test_help_names_the_commands(capsys):
    text = _help(capsys, ["--help"])
    assert text.startswith("usage: agentihooks agents [-h] {list,show} ...")
    assert "list List profile cards as JSON show Show one profile card as JSON" in text
    text = _help(capsys, ["list", "--help"])
    assert text.startswith("usage: agentihooks agents list [-h] [--capability CAPABILITY]")
    assert "--capability CAPABILITY Require this declared capability" in text


def test_missing_and_malformed_cards_are_errors(catalogue, capsys):
    with pytest.raises(ValueError) as error:
        cli().show_card("absent")
    assert str(error.value) == "Unknown agent profile: absent"
    with pytest.raises(SystemExit) as exc:
        cli().main(["show", "absent"])
    assert exc.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == "agentihooks agents: error: Unknown agent profile: absent"
    path = catalogue("bundle", "broken")
    (path / "profile.yml").write_text("description: 3\n")
    with pytest.raises(SystemExit) as exc:
        cli().main(["list"])
    assert exc.value.code == 2
    assert (
        capsys.readouterr().err.splitlines()[-1]
        == "agentihooks agents: error: profile broken description must be a string"
    )


@pytest.mark.parametrize("args", [[], ["unknown"], ["show"], ["list", "--capability"]])
def test_invalid_arguments_are_refused(args):
    with pytest.raises(SystemExit) as exc:
        cli().main(args)
    assert exc.value.code == 2


def test_installer_delegates_agents(monkeypatch):
    module = cli()
    monkeypatch.setattr("sys.argv", ["agentihooks", "agents", "list"])
    calls = []
    monkeypatch.setattr(module, "main", lambda args: calls.append(args) or 0)
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 0
    assert calls == [["list"]]
