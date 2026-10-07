import json
import subprocess

import pytest
import yaml

from hooks.context import profile_chain
from scripts import install
from scripts.profiles import scaffold


@pytest.fixture(autouse=True)
def _no_bundle_env(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_BUNDLE_PATH", raising=False)


def _bundle(tmp_path, capsys):
    target = tmp_path / "my-bundle"
    assert scaffold.main(["bundle", "new", str(target)]) == 0
    capsys.readouterr()
    return target


def test_bundle_new_lays_out_the_documented_bundle(tmp_path):
    target = tmp_path / "my-bundle"

    assert scaffold.main(["bundle", "new", str(target)]) == 0

    assert json.loads((target / "enforcements.json").read_text()) == {"enforcements": []}
    assert json.loads((target / ".claude" / ".mcp.json").read_text()) == {"mcpServers": {}}
    for folder in ("skills", "agents", "commands", "rules", "conditions"):
        assert (target / ".claude" / folder / ".gitkeep").is_file()
    assert (target / "profiles" / ".gitkeep").is_file()
    assert (target / "README.md").is_file()


def test_bundle_new_runs_git_init_and_links_the_bundle(tmp_path):
    target = tmp_path / "my-bundle"

    scaffold.main(["bundle", "new", str(target)])

    inside = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
    )
    assert inside.stdout.strip() == "true"
    assert (target / ".git").is_dir()
    assert json.loads(install.STATE_JSON.read_text())["bundle"]["path"] == str(target.resolve())
    assert install.STATE_JSON.resolve().is_relative_to(tmp_path.resolve())


def test_bundle_new_refuses_a_folder_that_holds_files(tmp_path, capsys):
    target = tmp_path / "taken"
    target.mkdir()
    (target / "keep.txt").write_text("mine")

    assert scaffold.main(["bundle", "new", str(target)]) == 1

    assert "not empty" in capsys.readouterr().err
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
    assert not install.STATE_JSON.exists()


def test_bundle_new_fills_an_empty_existing_folder(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()

    assert scaffold.main(["bundle", "new", str(target)]) == 0

    assert (target / "profiles").is_dir()


def test_overlay_new_writes_a_skeleton_into_the_linked_bundle(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)

    assert scaffold.main(["overlay", "new", "backtest-tuner", "--wears", "engineer,qa"]) == 0

    overlay = bundle / "profiles" / "backtest-tuner"
    manifest = yaml.safe_load((overlay / "profile.yml").read_text())
    assert manifest == {
        "name": "backtest-tuner",
        "description": "backtest-tuner overlay",
        "kind": "overlay",
        "wears": ["engineer", "qa"],
    }
    assert profile_chain.wears(overlay) == ["engineer", "qa"]
    assert (overlay / "CLAUDE.md").read_text().startswith("# backtest-tuner\n")
    assert json.loads((overlay / ".claude" / ".mcp.json").read_text()) == {"mcpServers": {}}
    for folder in ("skills", "rules"):
        assert (overlay / ".claude" / folder / ".gitkeep").is_file()
    assert scaffold.main(["overlay", "check", "backtest-tuner"]) == 0


def test_overlay_new_without_a_linked_bundle_names_the_fix(capsys):
    assert scaffold.main(["overlay", "new", "tuner", "--wears", "engineer"]) == 1

    assert "agentihooks bundle new" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "wears", "problem"),
    [
        ("tuner", "pilot", "pilot is not a base role"),
        ("tuner", "", "wears no base role"),
        ("a+b", "engineer", "cannot hold +"),
        ("engineer", "engineer", "engineer is a base role"),
        ("default", "engineer", "default is a built in profile"),
    ],
)
def test_overlay_new_refuses_a_bad_name_or_role(tmp_path, capsys, name, wears, problem):
    bundle = _bundle(tmp_path, capsys)

    assert scaffold.main(["overlay", "new", name, "--wears", wears]) == 1

    assert problem in capsys.readouterr().err
    assert not (bundle / "profiles" / name).exists()


def test_overlay_new_refuses_an_overlay_that_exists(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)
    scaffold.main(["overlay", "new", "tuner", "--wears", "engineer"])
    (bundle / "profiles" / "tuner" / "CLAUDE.md").write_text("mine")
    capsys.readouterr()

    assert scaffold.main(["overlay", "new", "tuner", "--wears", "qa"]) == 1

    assert "already exists" in capsys.readouterr().err
    assert (bundle / "profiles" / "tuner" / "CLAUDE.md").read_text() == "mine"


def _write_overlay(bundle, name, manifest):
    folder = bundle / "profiles" / name
    folder.mkdir(parents=True)
    (folder / "profile.yml").write_text(manifest)
    return folder


@pytest.mark.parametrize(
    ("manifest", "problems"),
    [
        ("name: tuner\nwears: [engineer]\n", ["kind must be overlay"]),
        ("name: tuner\nkind: overlay\nwears: engineer\n", ["wears must be a list of base roles"]),
        ("name: tuner\nkind: overlay\nwears: []\n", ["wears no base role"]),
        (
            "name: tuner\nkind: overlay\nwears: [engineer, pilot]\n",
            ["pilot is not a base role; pick from cicd, engineer, master, planner, qa"],
        ),
        (
            "name: tuner\nkind: overlay\nwears: [engineer]\nextends: [anton-base]\n",
            ["an overlay has no extends"],
        ),
        ("name: other\nkind: overlay\nwears: [engineer]\n", ["name other does not match the folder tuner"]),
        (
            "kind: profile\nwears: x\nextends: [a]\n",
            ["kind must be overlay", "wears must be a list of base roles", "an overlay has no extends"],
        ),
        ("- not\n- a mapping\n", ["profile.yml must be a mapping"]),
        (": : bad yaml [\n", ["profile.yml is not valid YAML"]),
    ],
)
def test_overlay_check_lists_every_problem(tmp_path, capsys, manifest, problems):
    bundle = _bundle(tmp_path, capsys)
    _write_overlay(bundle, "tuner", manifest)

    assert scaffold.main(["overlay", "check", "tuner"]) == 1

    assert scaffold.problems(bundle / "profiles" / "tuner") == problems
    err = capsys.readouterr().err
    assert all(problem in err for problem in problems)


def test_overlay_check_passes_a_valid_overlay(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)
    folder = _write_overlay(bundle, "tuner", "name: tuner\nkind: overlay\nwears: [engineer, planner]\n")

    assert scaffold.problems(folder) == []
    assert scaffold.main(["overlay", "check", "tuner"]) == 0
    assert "tuner" in capsys.readouterr().out


def test_overlay_check_names_a_missing_overlay_or_manifest(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)
    (bundle / "profiles" / "bare").mkdir()

    assert scaffold.main(["overlay", "check", "absent"]) == 1
    assert "absent not found" in capsys.readouterr().err
    assert scaffold.problems(bundle / "profiles" / "bare") == ["profile.yml is missing"]


def test_base_roles_are_the_package_roles():
    assert scaffold.base_roles() == ["cicd", "engineer", "master", "planner", "qa"]


def test_install_routes_the_scaffold_commands(monkeypatch):
    seen = []
    monkeypatch.setattr(scaffold, "main", lambda argv: seen.append(argv) or 0)

    for argv in (["bundle", "new", "x"], ["overlay", "check", "y"]):
        monkeypatch.setattr("sys.argv", ["agentihooks", *argv])
        with pytest.raises(SystemExit) as stop:
            install.main()
        assert stop.value.code == 0

    assert seen == [["bundle", "new", "x"], ["overlay", "check", "y"]]
