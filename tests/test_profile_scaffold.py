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


ROLES = ", ".join(scaffold.base_roles())


def _bundle(tmp_path, capsys):
    target = (tmp_path / "my-bundle").resolve()
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
    target = (tmp_path / "taken").resolve()
    target.mkdir()
    (target / "keep.txt").write_text("mine")

    assert scaffold.main(["bundle", "new", str(target)]) == 1

    assert capsys.readouterr().err == f"ERROR: {target} is not empty; pick a new folder for the bundle\n"
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]
    assert not install.STATE_JSON.exists()


def test_bundle_new_refuses_a_file(tmp_path, capsys):
    target = (tmp_path / "taken").resolve()
    target.write_text("mine")

    assert scaffold.main(["bundle", "new", str(target)]) == 1

    assert capsys.readouterr().err == f"ERROR: {target} is a file; pick a new folder for the bundle\n"
    assert target.read_text() == "mine"
    assert not install.STATE_JSON.exists()


def test_bundle_new_runs_git_init_before_writing_so_a_failed_init_can_be_retried(tmp_path, monkeypatch):
    target = tmp_path / "my-bundle"

    def refuse(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(scaffold.subprocess, "run", refuse)

    with pytest.raises(subprocess.CalledProcessError):
        scaffold.main(["bundle", "new", str(target)])

    assert list(target.iterdir()) == []
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
    assert capsys.readouterr().out == (
        f"[OK] Overlay backtest-tuner at {overlay}, worn by engineer, qa; commit it before a swarm wears it\n"
    )
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

    assert capsys.readouterr().err == "ERROR: no bundle linked; run agentihooks bundle new DIR first\n"


@pytest.mark.parametrize(
    ("name", "wears", "problem"),
    [
        ("tuner", "pilot", f"pilot is not a base role; pick from {ROLES}"),
        (
            "tuner",
            "engineer,pilot,ghost",
            f"pilot is not a base role; pick from {ROLES}; ghost is not a base role; pick from {ROLES}",
        ),
        ("tuner", " , ", "wears no base role"),
        ("a+b", "engineer", "overlay name a+b cannot hold +"),
        ("engineer", "engineer", "engineer is a base role; an overlay needs its own name"),
        ("default", "engineer", "default is a built in profile and would shadow the overlay"),
        ("../x", "engineer", "overlay name ../x must be a plain folder name"),
        ("a/b", "engineer", "overlay name a/b must be a plain folder name"),
        ("..", "engineer", "overlay name .. must be a plain folder name"),
        (".", "engineer", "overlay name . must be a plain folder name"),
        ("", "engineer", "overlay name  must be a plain folder name"),
    ],
)
def test_overlay_new_refuses_a_bad_name_or_role(tmp_path, capsys, name, wears, problem):
    bundle = _bundle(tmp_path, capsys)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if ".git" not in p.parts)

    assert scaffold.main(["overlay", "new", name, "--wears", wears]) == 1

    assert capsys.readouterr().err == f"ERROR: {problem}\n"
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if ".git" not in p.parts) == before
    assert (bundle / "profiles").is_dir()


def test_overlay_new_refuses_an_overlay_that_exists(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)
    scaffold.main(["overlay", "new", "tuner", "--wears", "engineer"])
    (bundle / "profiles" / "tuner" / "CLAUDE.md").write_text("mine")
    capsys.readouterr()

    assert scaffold.main(["overlay", "new", "tuner", "--wears", "qa"]) == 1

    folder = bundle / "profiles" / "tuner"
    assert capsys.readouterr().err == f"ERROR: overlay tuner already exists at {folder}\n"
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
            [f"pilot is not a base role; pick from {ROLES}"],
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
    assert capsys.readouterr().err == "ERROR: overlay tuner: " + "; ".join(problems) + "\n"


def test_overlay_check_passes_a_valid_overlay(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)
    folder = _write_overlay(bundle, "tuner", "name: tuner\nkind: overlay\nwears: [engineer, planner]\n")

    assert scaffold.problems(folder) == []
    assert scaffold.main(["overlay", "check", "tuner"]) == 0
    assert capsys.readouterr().out == "[OK] Overlay tuner is valid\n"


def test_overlay_check_names_a_missing_overlay_or_manifest(tmp_path, capsys):
    bundle = _bundle(tmp_path, capsys)
    (bundle / "profiles" / "bare").mkdir()

    assert scaffold.main(["overlay", "check", "absent"]) == 1
    assert capsys.readouterr().err == "ERROR: overlay absent not found in the linked bundle\n"
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


def test_install_refuses_an_unknown_bundle_action(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agentihooks", "bundle", "frobnicate"])

    with pytest.raises(SystemExit) as stop:
        install.main()

    assert stop.value.code == 2
    err = " ".join(capsys.readouterr().err.split()).replace("'", "")
    assert "invalid choice: frobnicate (choose from new, link, unlink, list, pull)" in err
